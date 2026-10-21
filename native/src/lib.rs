//! Severian native vector-search acceleration — exact top-k kernel.
//!
//! Score contract (Rust follow-up plan R0.2): dot(query, row) computed
//! in f64 after f32 little-endian BLOB decode, summed with Shewchuk
//! compensated summation — bit-identical to CPython `math.fsum`, which
//! is now the pure-Python reference in vector.py. Total tiebreak order:
//! score DESC, content ASC, memory_id ASC. No scope logic, no SQLite,
//! no domain logic — Python owns those.
//!
//! Production hardening (plan R1.2):
//! - `#![forbid(unsafe_code)]`;
//! - no `.unwrap()` / panic-reliant input path — every error returns a
//!   typed `PyValueError`, never a panic across FFI;
//! - strict empty-query, dimension, malformed-BLOB and finite-value
//!   validation;
//! - no per-row decoded `Vec<f64>` allocation (fused decode+product
//!   stream);
//! - GIL released around CPU work;
//! - exact deterministic total ordering via `f64::total_cmp`;
//! - `k=0` (empty), `k>N` (full sort), empty rows and all-tie rows
//!   defined explicitly;
//! - architecture flags chosen for parity (`x86-64`, see Cargo.toml).

#![forbid(unsafe_code)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn finite_f64(value: f64) -> PyResult<f64> {
    if !value.is_finite() {
        return Err(PyValueError::new_err("vector contains NaN or infinity"));
    }
    Ok(value)
}

/// CPython-compatible accurate summation (Shewchuk / ``math.fsum``).
///
/// The partials are collapsed from largest to smallest with CPython's
/// half-even correction. A left-to-right final sum loses valid low-order
/// information and breaks the strict Python/Rust score contract.
fn fsum(values: impl Iterator<Item = f64>, partials: &mut Vec<f64>) -> Result<f64, String> {
    partials.clear();
    for mut x in values {
        let n = partials.len();
        let mut i = 0usize;
        for j in 0..n {
            let mut y = partials[j];
            if x.abs() < y.abs() {
                std::mem::swap(&mut x, &mut y);
            }
            let hi = x + y;
            let yr = hi - x;
            let lo = y - yr;
            if lo != 0.0 {
                partials[i] = lo;
                i += 1;
            }
            x = hi;
        }
        partials.truncate(i);
        if x != 0.0 {
            if !x.is_finite() {
                return Err("non-finite dot product".to_string());
            }
            partials.push(x);
        }
    }

    let Some(mut hi) = partials.pop() else {
        return Ok(0.0);
    };
    let mut lo = 0.0;
    while let Some(y) = partials.pop() {
        let x = hi;
        hi = x + y;
        let yr = hi - x;
        lo = y - yr;
        if lo != 0.0 {
            break;
        }
    }
    if let Some(next) = partials.last() {
        if (lo < 0.0 && *next < 0.0) || (lo > 0.0 && *next > 0.0) {
            let y = lo * 2.0;
            let x = hi + y;
            let yr = x - hi;
            if y == yr {
                hi = x;
            }
        }
    }
    Ok(hi)
}

/// Exact bounded top-k.
///
/// rows: iterable of (memory_id: str, content: str, packed_vector: bytes)
/// where packed_vector is little-endian f32, length == len(query) * 4.
///
/// Returns [(memory_id, score)] sorted by (score DESC, content ASC,
/// memory_id ASC) — identical to the Python total-order key.
///
/// Edge cases (explicit contract):
/// - `k == 0` -> empty result (no scoring performed);
/// - `k > len(rows)` -> all rows, fully sorted;
/// - empty rows -> empty result;
/// - all rows tied -> content ASC then memory_id ASC, deterministic.
#[pyfunction]
#[pyo3(signature = (query, rows, k))]
fn exact_top_k(
    query: Vec<f64>,
    rows: Vec<(String, String, Vec<u8>)>,
    k: usize,
) -> PyResult<Vec<(String, f64)>> {
    let dim = query.len();
    if dim == 0 {
        return Err(PyValueError::new_err("query vector is empty"));
    }
    for v in &query {
        finite_f64(*v)?;
    }
    if k == 0 || rows.is_empty() {
        return Ok(Vec::new());
    }

    // Validate dims + finiteness cheaply first (no allocation).
    for (_mid, _content, blob) in &rows {
        if blob.len() != dim * 4 {
            return Err(PyValueError::new_err(format!(
                "packed vector length {} != dimension {} * 4",
                blob.len(),
                dim
            )));
        }
    }

    // Decode + score, releasing the GIL for the pure CPU pass.
    // The fused decode+product stream avoids any per-row decoded vector.
    // The reusable partials buffer keeps CPython-compatible fsum state out
    // of the allocation hot path.
    // Plain-Rust error inside the detached closure (PyErr is not Ungil);
    // converted to PyValueError after the GIL is reacquired.
    let mut scored: Vec<(f64, String, String)> = {
        let q = &query;
        let result: Result<Vec<(f64, String, String)>, String> = pyo3::Python::attach(|py| {
            py.detach(move || {
                let mut out = Vec::with_capacity(rows.len());
                let mut partials = Vec::with_capacity(64);
                for (mid, content, blob) in rows {
                    // NaN/inf via integer bit-test (exponent all ones,
                    // 0x7F800000) — cheaper than float classify. The
                    // stream is lazy, so the flag is final only after
                    // fsum consumes it; check AFTER the summation.
                    let mut nan = false;
                    let products =
                        blob.as_chunks::<4>()
                            .0
                            .iter()
                            .zip(q.iter())
                            .map(|(chunk, a)| {
                                let bits = u32::from_le_bytes(*chunk);
                                if bits & 0x7F80_0000 == 0x7F80_0000 {
                                    nan = true;
                                }
                                let b = f32::from_bits(bits) as f64;
                                a * b
                            });
                    let score = fsum(products, &mut partials)?;
                    if nan {
                        return Err("vector contains NaN or infinity".to_string());
                    }
                    out.push((score, content, mid));
                }
                Ok(out)
            })
        });
        result.map_err(PyValueError::new_err)?
    };

    // Total order key identical to Python: (-score, content, memory_id).
    // f64::total_cmp makes the sort fully deterministic even for -0.0.
    let by_key = |a: &(f64, String, String), b: &(f64, String, String)| {
        b.0.total_cmp(&a.0) // score DESC
            .then_with(|| a.1.cmp(&b.1)) // content ASC
            .then_with(|| a.2.cmp(&b.2)) // memory_id ASC
    };

    if k < scored.len() {
        // Exact bounded selection: full determinism, no approximate
        // partitioning (select_nth_unstable_by uses the same total key
        // and is safe with forbid(unsafe_code)).
        scored.select_nth_unstable_by(k, by_key);
        scored.truncate(k);
    }
    scored.sort_unstable_by(by_key);

    Ok(scored
        .into_iter()
        .map(|(score, _content, mid)| (mid, score))
        .collect())
}

/// Return the native module version string (diagnostics contract R2.3).
#[pyfunction]
fn native_version() -> String {
    format!(
        "severian-native-{}-pyo3-{}",
        env!("CARGO_PKG_VERSION"),
        option_env!("SEVERIAN_PYO3_VERSION").unwrap_or("unknown"),
    )
}

#[pymodule]
fn _severian_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(exact_top_k, m)?)?;
    m.add_function(wrap_pyfunction!(native_version, m)?)?;
    Ok(())
}

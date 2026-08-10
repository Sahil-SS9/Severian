//! Emits the resolved pyo3 version as a compile-time env var so the
//! native_version() diagnostics string is always truthful (R2.3).
//!
//! Zero-debt (P2-I): no `unwrap()`. The build script returns `Result`
//! and propagates hard errors with `?` so Cargo fails the build with an
//! explicit message instead of panicking.
use std::env;
use std::fs;
use std::path::Path;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=Cargo.lock");
    let manifest_dir_str = env::var("CARGO_MANIFEST_DIR")?;
    let manifest_dir = Path::new(&manifest_dir_str);
    let lock = manifest_dir.join("Cargo.lock");
    if let Ok(text) = fs::read_to_string(&lock) {
        // Find the package block named pyo3, then read its version.
        let mut found = false;
        for line in text.lines() {
            let t = line.trim();
            if t == "name = \"pyo3\"" {
                found = true;
            } else if found && t.starts_with("version = ") {
                let v = t.trim_start_matches("version = ").trim_matches('"');
                println!("cargo:rustc-env=SEVERIAN_PYO3_VERSION={v}");
                break;
            }
        }
    }
    Ok(())
}

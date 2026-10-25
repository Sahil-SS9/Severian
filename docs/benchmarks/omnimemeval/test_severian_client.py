"""Offline Severian adapter contract tests: real Severian package, hash embeddings, no GPU or model calls.
Skipped where Severian is not installed (the upstream CI image)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

try:
    from severian.infrastructure.vector import HashEmbedding
except ImportError:  # pragma: no cover - upstream CI has no Severian
    HashEmbedding = None

from scripts.client_factory import registry, severian_client

ENV = Path(__file__).resolve().parents[3] / ".env.severian-p0"


@unittest.skipIf(HashEmbedding is None, "severian is not installed")
class TestSeverianClient(unittest.TestCase):
    def setUp(self):
        assert HashEmbedding is not None  # guaranteed by skipIf
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        settings = {k: v for k, v in dotenv_values(ENV, interpolate=False).items() if v is not None}
        settings.update(SEVERIAN_OMNI_DB_ROOT=str(self.root / "stores"), SEVERIAN_RERANK="",
                        SEVERIAN_OMNI_EMBED_PATH="unused-hash-embedding")
        env = patch.dict(os.environ, settings, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_EMBEDDER", severian_client._LockedEmbedding(HashEmbedding())), ("_USER_LOCKS", {})):
            p = patch.object(severian_client, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = registry.create_client("severian")

    def chat(self, user, assistant, when="2023-05-08T13:56:00+00:00"):
        return [{"role": "user", "content": user, "chat_time": when},
                {"role": "assistant", "content": assistant, "chat_time": when}]

    def test_registered(self):
        self.assertIn("severian", registry.SUPPORTED_LIBS)
        self.assertIsInstance(self.client, severian_client.SeverianClient)

    def test_add_then_search_returns_the_dated_turn(self):
        self.client.add(self.chat("My favourite drink is green tea.", "Noted, green tea."), "u1")
        ctx = self.client.search("favourite drink", "u1", 20)
        self.assertIn("green tea", ctx)
        self.assertIn("[2023/05/08 (Mon) 13:56]", ctx)

    def test_users_are_isolated_and_case_variants_distinct(self):
        self.client.add(self.chat("My cat is called Miso.", "Lovely name."), "CaseUser")
        self.client.add(self.chat("I drive a red bicycle.", "Nice."), "caseuser")
        self.assertIn("Miso", self.client.search("cat name", "CaseUser", 10))
        self.assertNotIn("Miso", self.client.search("cat name", "caseuser", 10))
        self.client.delete("caseuser")
        self.assertEqual(self.client.search("bicycle", "caseuser", 10), "")
        self.assertIn("Miso", self.client.search("cat name", "CaseUser", 10))

    def test_unknown_user_and_bad_ids(self):
        self.assertEqual(self.client.search("anything", "nobody", 10), "")
        for bad in ("a/b", "", "-x", "a" * 201):
            with self.assertRaises(ValueError):
                self.client.search("q", bad, 10)

    def test_character_cap_and_limit_settings(self):
        for i in range(30):  # ~1.5k chars each: 10 items exceed the 8,000-char cap, so it must bind
            self.client.add(self.chat(f"Fact number {i}: I visited city {i} " + "x" * 1500, "ok"), "u2")
        native = self.client.search("visited city", "u2", 20)
        self.assertLessEqual(len(native), 8000)
        self.assertGreater(len(native), 4000)
        with patch.dict(os.environ, {"SEVERIAN_OMNI_MAX_CHARS": "0", "SEVERIAN_OMNI_LIMIT": "harness"}):
            wide = registry.create_client("severian")
        self.assertGreater(len(wide.search("visited city", "u2", 20)), 20000)

    def test_sessions_counted_and_assistant_only_turns_kept(self):
        self.client.add(self.chat("first", "one"), "u3")
        self.client.add([{"role": "assistant", "content": "assistant-only remark about kites",
                          "chat_time": "2023-06-01T10:00:00Z"}], "u3")
        self.assertEqual((self.client._dir("u3") / "sessions.txt").read_text(), "2")
        self.assertIn("kites", self.client.search("kites", "u3", 10))

    def test_helpers(self):
        self.assertEqual(severian_client._label("2023-05-08T13:56:00Z")[0], "2023/05/08 (Mon) 13:56")
        self.assertEqual(severian_client._label("not a date"), (None, None))
        pairs = severian_client._pairs([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                        {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}])
        self.assertEqual([(u and u["content"], r and r["content"]) for u, r in pairs],
                         [("a", "b"), (None, "c"), ("d", None)])


if __name__ == "__main__":
    unittest.main()

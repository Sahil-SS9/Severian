import unittest
from unittest.mock import patch

from severian.application.userfacts import FactCallError, FactExtractor


class FailureWarnings(unittest.TestCase):
    def test_every_failed_call_warns_including_retry(self):
        extractor = FactExtractor(sleep=lambda _: None)
        error = FactCallError("no JSON object or list in the reply", reply="[1] plain text")
        with patch.object(extractor, "_request", side_effect=error):
            with self.assertLogs("severian.application.userfacts", level="WARNING") as logs:
                result = extractor.extract(["I have a bike."])
        self.assertEqual(extractor.calls, 2)
        self.assertEqual(extractor.failures, 2)
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(result.failed)


if __name__ == "__main__":
    unittest.main()

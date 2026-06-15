from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.server.app import _agent_scoped_session_id


class ServerTests(unittest.TestCase):
    def test_session_id_is_scoped_by_agent_subject(self) -> None:
        coder = _agent_scoped_session_id("mcon.agent.coder", "session-1")
        auditor = _agent_scoped_session_id("mcon.agent.auditor", "session-1")

        self.assertNotEqual(coder, auditor)
        self.assertEqual(coder, "mcon_agent_coder--session-1")
        self.assertEqual(auditor, "mcon_agent_auditor--session-1")


if __name__ == "__main__":
    unittest.main()

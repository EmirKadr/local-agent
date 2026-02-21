import unittest

from bot import _extract_direct_tool_call, _handle_agent_mode, should_activate_agent_mode


class RouterModeTests(unittest.TestCase):
    def test_fact_question_stays_in_llm_mode(self):
        self.assertFalse(should_activate_agent_mode("vad är vattnets kokpunkt"))

    def test_product_volume_question_stays_in_llm_mode(self):
        self.assertFalse(should_activate_agent_mode("hur mycket finns i en imsdal flaska"))

    def test_script_request_activates_agent_mode(self):
        self.assertTrue(should_activate_agent_mode("kör script X och summera output"))


class _DummyMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class _DummyUpdate:
    def __init__(self, chat_id=1):
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.message = _DummyMessage()


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_negative_answer_stops_without_repeated_question(self):
        update = _DummyUpdate(chat_id=999)
        session = {
            "history": [],
            "vars": {},
            "last_tool": None,
            "step": 0,
            "mode": "agent",
            "agent_engine": "local",
        }

        await _handle_agent_mode(update, "nej", session)

        self.assertEqual(update.message.replies, ["Okej, då stannar vi här."])
        self.assertEqual(session["history"][-1]["content"], "Okej, då stannar vi här.")


class DirectToolRoutingTests(unittest.TestCase):
    def test_direct_tool_call_uses_defaults(self):
        tools = [
            {
                "name": "cars_lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "headless": {"type": "boolean", "default": True},
                        "write_file": {"type": "boolean", "default": False},
                    },
                },
            }
        ]

        tool_name, payload = _extract_direct_tool_call("kör cars_lookup", tools)

        self.assertEqual(tool_name, "cars_lookup")
        self.assertEqual(payload, {"headless": True, "write_file": False})

    def test_direct_tool_call_applies_text_overrides(self):
        tools = [
            {
                "name": "cars_lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "headless": {"type": "boolean", "default": True},
                        "write_file": {"type": "boolean", "default": False},
                    },
                },
            }
        ]

        tool_name, payload = _extract_direct_tool_call("start cars_lookup och spara fil visa browser", tools)

        self.assertEqual(tool_name, "cars_lookup")
        self.assertEqual(payload, {"headless": False, "write_file": True})


if __name__ == "__main__":
    unittest.main()

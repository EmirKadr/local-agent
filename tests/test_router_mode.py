import unittest

from bot import _handle_agent_mode, should_activate_agent_mode


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


if __name__ == "__main__":
    unittest.main()

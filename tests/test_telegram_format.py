import unittest

from services.telegram_format import build, format_bot_reply, format_notification


class TelegramFormatTests(unittest.TestCase):
    def test_plain_notification_is_escaped_and_has_severity_header(self):
        rendered = format_notification(
            "Order update",
            "Price: < 100 & status: ready",
            level="SUCCESS",
        )
        self.assertIn("✅ <b>Order update</b>", rendered)
        self.assertIn("Price: &lt; 100 &amp; status: ready", rendered)

    def test_html_builder_payload_is_preserved(self):
        payload = build(
            title="Order filled",
            emoji="🎯",
            fields=[("Instrument", "NIFTY 24000 CE")],
        )
        rendered = format_notification(payload, level="INFO")
        self.assertIn("<b>Order filled</b>", rendered)
        self.assertIn("<code>NIFTY 24000 CE</code>", rendered)
        self.assertIn("<i>ℹ️ INFO</i>", rendered)

    def test_legacy_multiline_payload_is_normalized(self):
        payload = build(
            "🎯",
            "ORDER UPDATE",
            fields=[("Price", "100.25")],
            status="success",
        )
        rendered = format_notification(payload, level="SUCCESS")
        self.assertIn("<b>ORDER UPDATE</b>", rendered)
        self.assertIn("<b>Price:</b>", rendered)
        self.assertNotIn("━━━━━━━━", rendered)

    def test_bot_reply_escapes_untrusted_text(self):
        rendered = format_bot_reply("Value < 100 & token is safe")
        self.assertIn("🤖 <b>MARKET ENGINE</b>", rendered)
        self.assertIn("Value &lt; 100 &amp; token is safe", rendered)


if __name__ == "__main__":
    unittest.main()

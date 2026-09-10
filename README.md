# Free Advertisement Verification Bot

A Telegram bot that lets channel owners apply for free advertising of a Mini App. It automatically verifies subscriber count, average views, presence of the official referral link, and a 24-hour view-tracking requirement on the submitted post -- then routes every application to an admin for the final approve/reject decision. **The bot never auto-approves a channel.**

## Project layout

```text
bot/
  config.py
  models.py
  database.py
  telethon_client.py
  verification.py
  notifications.py
  keyboards.py
  scheduler.py
  handlers/
    user.py
    admin.py
  main.py
tests/
  test_verification.py
```

See the source and `.env.example` for configuration and deployment details.

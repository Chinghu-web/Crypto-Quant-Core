# core/notifier.py
import os, requests
from typing import List, Dict, Any

def tg_send(cfg: Dict[str, Any], title: str, lines: List[str]) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or ((cfg.get("telegram") or {}).get("bot_token") or "")
    chats = (cfg.get("telegram") or {}).get("chat_id") or []
    if not token or not chats:
        print(f"[NOTIFY:OFF] {title}\n" + "\n".join(lines))
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"*{title}*\n" + "\n".join(lines)
    ok = True
    for chat in chats:
        try:
            r = requests.post(url, json={"chat_id": chat, "text": text, "parse_mode":"Markdown"}, timeout=10)
            ok = ok and r.ok
        except Exception as e:
            ok = False
            print("[TG_ERR]", e)
    return ok

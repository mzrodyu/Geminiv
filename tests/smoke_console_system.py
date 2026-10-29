import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="v2o-state-")
os.environ["STATE_DIR"] = STATE
os.environ["API_KEY"] = "testpass123"
os.environ.pop("VERTEX_EXPRESS_API_KEY", None)
os.environ.pop("PROXY_URL", None)
sys.path.insert(0, os.path.join(ROOT, "app"))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


async def _fake_refresh() -> bool:
    return True


async def _fake_models():
    return ["gemini-test-a", "gemini-test-b"]


main.refresh_models_config_cache = _fake_refresh
main.get_express_models = _fake_models

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


with TestClient(main.app) as c:
    # 1) 未登录首页：登录页 + 首次启动提示
    r = c.get("/")
    check("login page 200", r.status_code == 200)
    check("login shows first-run hint", "首次启动" in r.text and "firstrun" in r.text)
    check("placeholder replaced", "__HINT_HTML__" not in r.text)

    # 2) 错误口令
    r = c.post("/api/login", json={"password": "nope"})
    check("wrong password 401", r.status_code == 401)

    # 3) 正确口令 -> 控制台
    r = c.post("/api/login", json={"password": "testpass123"})
    check("login ok", r.status_code == 200 and r.json().get("ok") is True)
    r = c.get("/")
    check("dashboard served", "系统设置" in r.text and 'id="wizard"' in r.text and 'id="view-system"' in r.text)

    # 4) /api/system
    s = c.get("/api/system").json()
    check("system first_run", s["first_run"] is True)
    check("system key not default", s["api_key_is_default"] is False and s["api_key_masked"] != "testpass123")
    check("system state dir", s["state_dir"].endswith(os.path.basename(STATE)), s["state_dir"])
    check("system models count", s["models_count"] == 2, str(s["models_count"]))

    # 5) 口令太短 / 默认口令
    check("short key rejected", c.post("/api/system/api-key", json={"api_key": "abc"}).status_code == 400)
    check("default key rejected", c.post("/api/system/api-key", json={"api_key": "123456"}).status_code == 400)
    check("mismatch rejected", c.post("/api/system/api-key", json={"api_key": "abcdefgh1", "confirm_api_key": "abcdefgh2"}).status_code == 400)

    # 6) 正常改口令：旧会话失效 + 当前会话续期
    r = c.post("/api/system/api-key", json={"api_key": "NewPassw0rd!", "confirm_api_key": "NewPassw0rd!"})
    check("change key ok", r.status_code == 200, r.text[:120])
    check("session renewed", c.get("/api/system").status_code == 200)

    # 7) 旧口令不能登录、新口令可以
    c2 = TestClient(main.app)
    check("old password rejected", c2.post("/api/login", json={"password": "testpass123"}).status_code == 401)

    # 8) 代理校验 + 保存
    check("bad proxy rejected", c.post("/api/system/proxy", json={"proxy_url": "127.0.0.1:7890"}).status_code == 400)
    r = c.post("/api/system/proxy", json={"proxy_url": "socks5://user:pass@127.0.0.1:1080"})
    check("proxy saved", r.status_code == 200 and main.config.PROXY_URL == "socks5://user:pass@127.0.0.1:1080")
    r = c.post("/api/system/proxy", json={"proxy_url": ""})
    check("proxy cleared", r.status_code == 200 and main.config.PROXY_URL is None, str(main.config.PROXY_URL))

    # 9) 模型源
    check("bad models url rejected", c.post("/api/system/models-source", json={"url": "ftp://x"}).status_code == 400)
    r = c.post("/api/system/models-source", json={"url": "https://example.com/m.json"})
    check("models url saved", r.status_code == 200 and r.json()["count"] == 2, r.text[:160])
    r = c.post("/api/system/models-source", json={"url": ""})
    check("models url reset", r.status_code == 200 and main.config.MODELS_CONFIG_URL == "")

    # 10) 状态备份
    r = c.get("/api/system/state-backup")
    check("backup attachment", r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""))
    check("backup has runtime", "NewPassw0rd!" in r.text)

    # 11) 标记首次启动完成
    check("setup complete", c.post("/api/system/setup-complete").json().get("setup_completed") is True)
    check("first_run false", c.get("/api/system").json()["first_run"] is False)

    # 12) 动态 API Key 生效于 /v1 鉴权
    h_old = {"Authorization": "Bearer testpass123"}
    h_new = {"Authorization": "Bearer NewPassw0rd!"}
    check("v1 old key 401", c.get("/v1/models", headers=h_old).status_code == 401)
    r = c.get("/v1/models", headers=h_new)
    check("v1 new key ok", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # 13) 无 cookie 访问受保护接口
    r = TestClient(main.app).get("/api/system")
    check("unauth blocked", r.status_code == 401)

print("\n==== RESULT ====")
failed = 0
for name, ok, extra in results:
    print(("PASS  " if ok else "FAIL  ") + name + (("   << " + extra) if (extra and not ok) else ""))
    failed += 0 if ok else 1
print(f"\n{len(results) - failed}/{len(results)} passed; state dir = {STATE}")
sys.exit(1 if failed else 0)

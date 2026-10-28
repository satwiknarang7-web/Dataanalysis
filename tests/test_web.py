"""The product end to end: accounts, data in, boards, library, sharing and the rules around them."""

import re

from sqlalchemy import update

from insight.db.models import User
from tests.conftest import sign_up


async def make_pro(client, email):
    state = client.app.state.insight
    async with state.sessions() as db:
        await db.execute(update(User).where(User.email == email).values(plan="pro"))
        await db.commit()
    await state.cache.backend.close()  # drop cached sessions so the new plan is read


async def open_sample(client, key="retail") -> str:
    r = await client.post(f"/samples/{key}")
    assert r.status_code == 303
    return r.headers["location"]


async def test_public_pages(client):
    for path in [
        "/",
        "/pricing",
        "/signin",
        "/signup",
        "/demo/retail/dashboard",
        "/demo/saas/ask",
        "/demo/people/quality",
        "/demo/retail/data",
        "/healthz",
    ]:
        r = await client.get(path)
        assert r.status_code == 200, path
    r = await client.get("/readyz")
    assert r.json()["database"] == "ok"
    assert (await client.get("/nope")).status_code == 404


async def test_security_headers_and_cross_site_writes(client):
    r = await client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    r = await client.post("/signin", data={"email": "a@b.co", "password": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


async def test_app_pages_need_sign_in(client):
    r = await client.get("/home")
    assert r.status_code == 303 and r.headers["location"].startswith("/signin?next=/home")


async def test_sign_up_sign_in_and_out(client):
    await sign_up(client)
    assert (await client.get("/home")).status_code == 200
    await client.post("/signout")
    assert (await client.get("/home")).status_code == 303
    r = await client.post("/signin", data={"email": "ANA@example.com", "password": "wrong password!", "next": "/home"})
    assert r.status_code == 400 and "match" in r.text
    r = await client.post("/signin", data={"email": "ANA@example.com", "password": "correct horse battery", "next": "//evil.example"})
    assert r.status_code == 303 and r.headers["location"] == "/home"  # never an open redirect


async def test_duplicate_and_weak_sign_ups(client):
    await sign_up(client)
    client.cookies.clear()
    r = await client.post("/signup", data={"email": "ana@example.com", "password": "another long one"})
    assert "already exists" in r.text
    r = await client.post("/signup", data={"email": "bo@example.com", "password": "short"})
    assert "10 characters" in r.text


async def test_sign_in_is_rate_limited(client):
    await sign_up(client)
    client.cookies.clear()
    for _ in range(8):
        await client.post("/signin", data={"email": "ana@example.com", "password": "nope nope nope"})
    r = await client.post("/signin", data={"email": "ana@example.com", "password": "correct horse battery"})
    assert "Too many attempts" in r.text


async def test_sample_to_dashboard_to_every_tab(client):
    await sign_up(client)
    board = await open_sample(client)
    r = await client.get(board)
    assert r.status_code == 200 and "Key findings" in r.text and "data-chart" in r.text
    base = board.rsplit("/", 1)[0]
    for tab in ["ask", "data", "quality"]:
        assert (await client.get(f"{base}/{tab}")).status_code == 200
    r = await client.post(f"{base}/ask", data={"question": "revenue by region"})
    assert "North" in r.text
    r = await client.get(f"{base}/export.csv")
    assert r.headers["content-type"].startswith("text/csv") and "order_id" in r.text


async def test_filters_change_the_board(client):
    await sign_up(client)
    board = await open_sample(client)
    full = await client.get(board, headers={"hx-target": "board"})
    north = await client.get(board + "?f.region=North", headers={"hx-target": "board"})
    assert "4,000 rows" in full.text
    assert re.search(r"[\d,]+ rows match your filters", north.text)
    ignored = await client.get(board + "?f.secret=1&f.region=Atlantis", headers={"hx-target": "board"})
    assert "4,000 rows" in ignored.text  # only offered fields and values are applied


async def test_upload_a_file(client):
    await sign_up(client)
    csv = "date,region,sales\n" + "\n".join(f"2024-0{1 + i % 9}-1{i % 9},{'North' if i % 2 else 'South'},{100 + i}" for i in range(60))
    r = await client.post("/upload", files={"file": ("my sales.csv", csv.encode(), "text/csv")}, headers={"x-requested-with": "fetch"})
    assert r.status_code == 200, r.text
    r = await client.get(r.json()["redirect"])
    assert "My sales" in r.text  # file names are tidied for display
    r = await client.post("/upload", files={"file": ("photo.png", b"\x89PNG", "image/png")}, headers={"x-requested-with": "fetch"})
    assert r.status_code == 400 and "CSV" in r.json()["error"]


async def test_datasets_are_private(client):
    await sign_up(client, "ana@example.com")
    board = await open_sample(client)
    client.cookies.clear()
    await sign_up(client, "bo@example.com")
    assert (await client.get(board)).status_code == 404
    dataset_id = board.split("/")[2]
    assert (await client.post(f"/datasets/{dataset_id}/delete")).status_code == 404


async def test_save_analysis_and_free_plan_limits(client):
    await sign_up(client)
    board = await open_sample(client)
    dataset_id = board.split("/")[2]
    r = await client.post(f"/datasets/{dataset_id}/save", data={"title": "Weekly North", "f.region": "North"})
    assert r.status_code == 204
    analysis = r.headers["hx-redirect"]
    page = await client.get(analysis)
    assert "Weekly North" in page.text and "match your filters" in page.text
    assert "Weekly North" in (await client.get("/library")).text
    for i in range(2):
        await client.post(f"/datasets/{dataset_id}/save", data={"title": f"n{i}"})
    r = await client.post(f"/datasets/{dataset_id}/save", data={"title": "one too many"})
    assert r.status_code == 402 and "Upgrade" in r.text
    r = await client.post(f"{analysis}/share", data={"email": "bo@example.com"})
    assert "part of Pro" in r.text


async def test_sharing_on_pro(client):
    await sign_up(client, "ana@example.com")
    await make_pro(client, "ana@example.com")
    board = await open_sample(client)
    dataset_id = board.split("/")[2]
    analysis = (await client.post(f"/datasets/{dataset_id}/save", data={"title": "Shared board"})).headers["hx-redirect"]
    r = await client.post(f"{analysis}/share", data={"email": "Bo@Example.com"})
    assert "bo@example.com" in r.text
    r = await client.post(f"/datasets/{dataset_id}/sql", data={"sql": "SELECT count(*) AS n FROM data"})
    assert "4000" in r.text

    client.cookies.clear()
    await sign_up(client, "bo@example.com")
    assert "Shared board" in (await client.get("/library")).text
    page = await client.get(analysis)
    assert page.status_code == 200 and "Shared with you" in page.text
    assert (await client.post(f"{analysis}/delete")).status_code == 404  # viewers can't delete
    r = await client.post(f"/datasets/{dataset_id}/sql", data={"sql": "SELECT 1"})
    assert "part of Pro" in r.text


async def test_deleting_a_dataset_removes_its_rows(client):
    await sign_up(client)
    board = await open_sample(client)
    dataset_id = board.split("/")[2]
    assert (await client.post(f"/datasets/{dataset_id}/delete")).status_code == 303
    assert (await client.get(board)).status_code == 404

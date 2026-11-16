"""Execute vault inspection/fill in a disposable Chromium with synthetic DOM only."""

from __future__ import annotations

import json
import os
import signal
import subprocess

import pytest
from websockets.sync.client import connect

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls,
)
from hermes_platform.resolver import locate_command
from tests.waiting import wait_until

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER") != "1", reason="opt in to disposable browser fixtures"),
]


@pytest.fixture
def dom(tmp_path):
    browser = next((r.command for name in ("chromium", "google-chrome", "chrome")
                    if (r := locate_command(name)).command), None)
    if browser is None:
        pytest.skip("Chromium is unavailable")
    counter = 0

    def run(setup, script):
        nonlocal counter
        counter += 1
        profile = tmp_path / f"profile-{counter}"
        with (tmp_path / f"browser-{counter}.log").open("w") as log:
            process = subprocess.Popen(
                [*browser, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                 "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                 "--disable-component-update", "--disable-default-apps", "--disable-sync",
                 "--host-resolver-rules=MAP * ~NOTFOUND", "--remote-debugging-address=127.0.0.1",
                 "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                port_file = profile / "DevToolsActivePort"
                wait_until(lambda: port_file.is_file() or process.poll() is not None,
                           what="disposable Chromium CDP readiness")
                assert process.poll() is None, f"Chromium exited {process.returncode}"
                port, path = port_file.read_text().splitlines()
                with connect(f"ws://127.0.0.1:{port}{path}", open_timeout=5) as ws:
                    return _evaluate_dom(ws, setup, script)
            finally:
                if process.poll() is None:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=10)

    return run


def _evaluate_dom(ws, setup, script):
    """Use bounded CDP replies rather than Chromium's unrelated dump-and-exit mode."""
    ident = 0

    def call(method, params=None, session=None):
        nonlocal ident
        ident += 1
        request = {"id": ident, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        ws.send(json.dumps(request))
        while True:
            response = json.loads(ws.recv(timeout=10))
            if response.get("id") == ident:
                assert "error" not in response, response
                return response["result"]

    target = call("Target.createTarget", {"url": "about:blank"})
    session = call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
    wait_until(lambda: call("Runtime.evaluate", {"expression": "!!document.body", "returnByValue": True},
                            session)["result"].get("value"), what="synthetic document body")
    expression = "(() => {" + setup + "\n" + script + "\n})()"
    result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
    assert "exceptionDetails" not in result, result
    return result["result"]["value"]


_NESTED_DOM = """
document.body.insertAdjacentHTML('beforeend', `
  <label id="label">Document label</label><input name="light" type="password">
  <input name="unrelated"><iframe srcdoc='<input name="frame" type="password">'></iframe>
  <div id="outer"></div><div id="closed"></div>`);
const outer = document.getElementById('outer').attachShadow({mode: 'open'});
outer.innerHTML = `<form><label id="label">Outer password</label>
  <input name="shadow" type="password" aria-labelledby="label">
  <select name="country" autocomplete="country"><option value="">Choose</option><option value="PL">Poland</option></select>
  </form><div id="inner"></div>`;
const inner = outer.getElementById('inner').attachShadow({mode: 'open'});
inner.innerHTML = '<span id="label">Nested password</span><input name="nested" type="password" aria-labelledby="label">';
const closed = document.getElementById('closed').attachShadow({mode: 'closed'});
closed.innerHTML = '<input name="closed" type="password">';
const events = [];
for (const type of ['input', 'change']) document.addEventListener(type, e => {
  events.push([type, e.composedPath()[0].name, e.composed]);
});
const roots = [document, outer, inner];
const values = () => roots.flatMap(r => Array.from(r.querySelectorAll('input,select'), e => [e.name, e.value]));
const stamps = () => roots.reduce((n, r) => n + r.querySelectorAll('[data-hermes-vault-slot]').length, 0);
"""


def test_nested_controls_labels_select_events_and_cleanup(dom):
    fills = [
        {"index": 2, "value": "synthetic-outer"},
        {"index": 3, "token": "country-name", "value": "Poland"},
        {"index": 4, "value": "synthetic-nested"},
        {"index": 1, "value": "must-not-fill-text"},
        {"index": 99, "value": "missing"},
    ]
    value = dom(_NESTED_DOM, f"""
        const controls = JSON.parse({build_inspection_js('inspection')});
        const result = JSON.parse({build_fill_js(fills, 'null', 'inspection')});
        return {{controls, result, values: values(), events, stamps: stamps(),
                 closed: closed.querySelector('input').value}};
    """)
    assert [c["name"].strip() for c in value["controls"]] == ["light", "unrelated", "shadow", "country", "nested"]
    assert value["controls"][2]["label"].strip() == "Outer password"
    assert value["controls"][4]["label"].strip() == "Nested password"
    assert value["controls"][2]["formIndex"] == value["controls"][3]["formIndex"] == 0
    assert value["result"] == {"filled": 3}
    assert dict(value["values"]) == dict(light="", unrelated="", shadow="synthetic-outer", country="PL", nested="synthetic-nested")
    assert value["events"] == [
        ["input", "shadow", True], ["change", "shadow", True],
        ["change", "country", True], ["input", "nested", True], ["change", "nested", True],
    ]
    assert value["stamps"] == 0
    assert value["closed"] == ""


def test_shadow_fill_still_requires_origin_nonce_and_password_type(dom):
    fills = [{"index": 4, "value": "synthetic-secret"}]
    value = dom(_NESTED_DOM, f"""
        {build_inspection_js('stale')};
        {build_inspection_js('fresh')};
        const nonce = JSON.parse({build_fill_js(fills, 'null', 'stale')});
        const afterNonce = values();
        const nonceStamps = stamps();
        {build_inspection_js('fresh')};
        const origin = JSON.parse({build_fill_js(fills, 'https://other.invalid', 'fresh')});
        const afterOrigin = values();
        inner.querySelector('input').type = 'text';
        const type = JSON.parse({build_fill_js(fills, 'null', 'fresh')});
        return {{nonce, afterNonce, nonceStamps, origin, afterOrigin, type, values: values(), stamps: stamps()}};
    """)
    assert value["nonce"] == value["type"] == {"filled": 0}
    assert value["origin"] == {"refused": "origin_changed", "found": "null"}
    assert all(v == "" for row in ["afterNonce", "afterOrigin", "values"] for _, v in value[row])
    assert value["nonceStamps"] == value["stamps"] == 0


@pytest.mark.parametrize("groups,boxes", [(2, 3), (1, 6)])
def test_formless_otp_digits_stay_in_one_shadow_scope(dom, groups, boxes):
    setup = f"""
        const roots = [];
        for (let group = 0; group < {groups}; group++) {{
          const host = document.body.appendChild(document.createElement('div'));
          const root = host.attachShadow({{mode: 'open'}});
          root.innerHTML = '<input autocomplete="one-time-code" maxlength="1">'.repeat({boxes});
          roots.push(root);
        }}
    """
    inspected = dom(setup, f"return {{controls: JSON.parse({build_inspection_js('otp')})}};")
    controls = [LoginControl.from_dict(c) for c in inspected["controls"]]
    assert len(controls) == groups * boxes
    assert len({c.form_index for c in controls}) == groups
    fills = build_otp_fills(classify_otp_controls(controls), "246810")
    if groups == 2:
        assert len(fills) == 1 and fills[0]["value"] == "246810"
    else:
        assert [f["value"] for f in fills] == list("246810")
        value = dom(setup, f"""
            {build_inspection_js('otp')};
            const result = JSON.parse({build_fill_js(fills, 'null', 'otp')});
            return {{result, digits: Array.from(roots[0].querySelectorAll('input'), e => e.value),
                     stamps: roots[0].querySelectorAll('[data-hermes-vault-slot]').length}};
        """)
        assert value == {"result": {"filled": 6}, "digits": list("246810"), "stamps": 0}

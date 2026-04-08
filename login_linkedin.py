#!/usr/bin/env python3

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from getpass import getpass

import websocket


DEBUG_PORT = 18800
DEBUG_BASE = f"http://127.0.0.1:{DEBUG_PORT}"
TARGETS_PATHS = ("/json/list", "/json")
LOGIN_URL_HINT = "linkedin.com/login"
SETTLE_TIMEOUT_SECONDS = 8.0
CONTROL_CANDIDATES_JS = r"""
(() => {
  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function rectSummary(el) {
    const rect = el.getBoundingClientRect();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      top: rect.top,
      left: rect.left,
      width: rect.width,
      height: rect.height
    };
  }

  function selectorSummary(el) {
    if (!el) return null;
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === Node.ELEMENT_NODE && depth < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        part += `#${node.id}`;
        parts.unshift(part);
        break;
      }
      const classNames = Array.from(node.classList).slice(0, 2);
      if (classNames.length) {
        part += "." + classNames.join(".");
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (child) => child.tagName === node.tagName
        );
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      node = parent;
      depth += 1;
    }
    return parts.join(" > ");
  }

  function xpathFor(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return null;
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) {
          index += 1;
        }
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return "/" + parts.join("/");
  }

  function textContent(el) {
    return [
      el.innerText || "",
      el.value || "",
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || ""
    ].join(" ").replace(/\s+/g, " ").trim();
  }

  function visibleLabel(el) {
    return [el.innerText || "", el.value || ""].join(" ").replace(/\s+/g, " ").trim();
  }

  function summarize(el, kind, rejectionReason) {
    return {
      kind,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").toLowerCase(),
      id: el.id || "",
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      ariaLabel: el.getAttribute("aria-label") || "",
      text: textContent(el).slice(0, 120),
      rect: rectSummary(el),
      visible: isVisible(el),
      rejected: Boolean(rejectionReason),
      rejectedReason: rejectionReason || "",
      confidence: rejectionReason ? "low" : "high",
      selection_reason: "",
      selector: selectorSummary(el),
      xpath: xpathFor(el)
    };
  }

  function choosePassword(passwords, texts) {
    const eligibleTexts = texts.filter((item) => item.visible);
    let best = null;
    for (const password of passwords.filter((item) => item.visible)) {
      const aboveTexts = eligibleTexts
        .map((text) => ({
          text,
          gap: password.rect.y - text.rect.y
        }))
        .filter((pair) => pair.gap >= 0 && pair.gap <= 260)
        .sort((a, b) => a.gap - b.gap || a.text.rect.y - b.text.rect.y);
      const score = aboveTexts.length ? aboveTexts[0].gap : 100000 + password.rect.y;
      const candidate = {
        password,
        pairedText: aboveTexts.length ? aboveTexts[0].text : null,
        score
      };
      if (!best || candidate.score < best.score) {
        best = candidate;
      }
    }
    return best;
  }

  function chooseText(texts, chosenPassword) {
    if (!chosenPassword) return null;
    const aboveTexts = texts
      .filter((item) => item.visible)
      .map((text) => ({
        text,
        gap: chosenPassword.rect.y - text.rect.y
      }))
      .filter((pair) => pair.gap >= 0 && pair.gap <= 260)
      .sort((a, b) => a.gap - b.gap || a.text.rect.y - b.text.rect.y);

    if (!aboveTexts.length) {
      return null;
    }

    const preferred = aboveTexts.filter((pair) => !pair.text.rejected);
    if (preferred.length) {
      preferred[0].text.confidence = "high";
      preferred[0].text.selection_reason = "preferred non-webauthn input above password";
      return preferred[0].text;
    }

    if (aboveTexts.length === 1 && aboveTexts[0].gap <= 140) {
      aboveTexts[0].text.confidence = "medium";
      aboveTexts[0].text.selection_reason = "sole visible text input above password within 140px fallback";
      return aboveTexts[0].text;
    }

    aboveTexts[0].text.confidence = "low";
    aboveTexts[0].text.selection_reason = "nearest visible text input above password fallback";
    return aboveTexts[0].text;
  }

  function chooseButton(buttons, chosenPassword) {
    const accepted = buttons.filter((item) => !item.rejected);
    if (!accepted.length) return null;
    if (!chosenPassword) return accepted[0];
    const below = accepted
      .map((button) => ({
        button,
        gap: button.rect.y - chosenPassword.rect.y
      }))
      .filter((pair) => pair.gap >= 0)
      .sort((a, b) => a.gap - b.gap || a.button.rect.y - b.button.rect.y);
    if (below.length) return below[0].button;
    return accepted
      .slice()
      .sort((a, b) => Math.abs(a.rect.y - chosenPassword.rect.y) - Math.abs(b.rect.y - chosenPassword.rect.y))[0];
  }

  const textCandidates = Array.from(document.querySelectorAll("input"))
    .filter((el) => {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return ["text", "email", "tel"].includes(type);
    })
    .map((el) => {
      const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
      let rejectionReason = "";
      if (!isVisible(el)) {
        rejectionReason = "not visible";
      } else if (autocomplete.includes("webauthn")) {
        rejectionReason = "autocomplete contains webauthn; lower-confidence fallback only";
      }
      return summarize(el, "textInput", rejectionReason);
    })
    .filter((item) => item.visible);

  const passwordCandidates = Array.from(document.querySelectorAll('input[type="password"]'))
    .map((el) => summarize(el, "passwordInput", isVisible(el) ? "" : "not visible"))
    .filter((item) => item.visible)
    .sort((a, b) => a.rect.y - b.rect.y);

  const buttonCandidates = Array.from(
    document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]')
  )
    .map((el) => {
      const label = visibleLabel(el);
      const combined = textContent(el);
      let rejectionReason = "";
      if (!isVisible(el)) {
        rejectionReason = "not visible";
      } else if (/(apple|google|continue|join)/i.test(combined)) {
        rejectionReason = "contains excluded text";
      } else if (label.trim() !== "Sign in") {
        rejectionReason = "text is not exact Sign in";
      }
      return summarize(el, "button", rejectionReason);
    })
    .filter((item) => item.visible);

  const chosenPasswordPair = choosePassword(passwordCandidates, textCandidates);
  const chosenPassword = chosenPasswordPair ? chosenPasswordPair.password : null;
  const chosenText = chooseText(textCandidates, chosenPassword);
  const chosenButton = chooseButton(buttonCandidates, chosenPassword);

  if (chosenPassword && !chosenPassword.selection_reason) {
    chosenPassword.selection_reason = "visible password input paired with nearest text input cluster";
  }
  if (chosenPassword && !chosenPassword.confidence) {
    chosenPassword.confidence = "high";
  }
  if (chosenButton && !chosenButton.selection_reason) {
    chosenButton.selection_reason = "exact visible Sign in button closest below password";
  }
  if (chosenButton && !chosenButton.confidence) {
    chosenButton.confidence = "high";
  }

  return {
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    candidates: {
      textInputs: textCandidates,
      passwordInputs: passwordCandidates,
      buttons: buttonCandidates
    },
    chosen: {
      textInput: chosenText,
      passwordInput: chosenPassword,
      signInButton: chosenButton
    }
  };
})()
"""


PAGE_STATE_JS = r"""
(() => {
  const active = document.activeElement;
  return {
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    activeTag: active ? active.tagName.toLowerCase() : null,
    activeType: active && active.getAttribute ? (active.getAttribute("type") || "").toLowerCase() : null,
    visibleTextInputs: Array.from(document.querySelectorAll("input")).filter((el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0 &&
        ["text", "email", "tel"].includes(type);
    }).length,
    visiblePasswordInputs: Array.from(document.querySelectorAll('input[type="password"]')).filter((el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0;
    }).length
  };
})()
"""


BODY_TEXT_JS = r"""
(() => (
  (document.body && document.body.innerText) ? document.body.innerText.replace(/\s+\n/g, "\n").trim().slice(0, 800) : ""
))()
"""


ACTIVE_ELEMENT_SUMMARY_JS = r"""
(() => {
  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function rectSummary(el) {
    const rect = el.getBoundingClientRect();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      top: rect.top,
      left: rect.left,
      width: rect.width,
      height: rect.height
    };
  }

  function xpathFor(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return null;
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) {
          index += 1;
        }
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return "/" + parts.join("/");
  }

  function selectorSummary(el) {
    if (!el) return null;
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === Node.ELEMENT_NODE && depth < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        part += `#${node.id}`;
        parts.unshift(part);
        break;
      }
      parts.unshift(part);
      node = node.parentElement;
      depth += 1;
    }
    return parts.join(" > ");
  }

  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return {
      focusedInput: false,
      activeTag: el ? el.tagName.toLowerCase() : null,
      activeType: el && el.getAttribute ? (el.getAttribute("type") || "").toLowerCase() : null,
      id: el && el.id ? el.id : "",
      autocomplete: el && el.getAttribute ? (el.getAttribute("autocomplete") || "").toLowerCase() : "",
      ariaLabel: el && el.getAttribute ? (el.getAttribute("aria-label") || "") : "",
      text: el ? ((el.innerText || el.value || "").replace(/\s+/g, " ").trim().slice(0, 120)) : "",
      valueLength: el && "value" in el ? (el.value || "").length : null,
      visible: !!el && isVisible(el),
      rect: el && el.getBoundingClientRect ? rectSummary(el) : null,
      selector: el ? selectorSummary(el) : null,
      xpath: el ? xpathFor(el) : null
    };
  }
  return {
    focusedInput: el.tagName === "INPUT",
    activeTag: el.tagName.toLowerCase(),
    activeType: el.getAttribute ? (el.getAttribute("type") || "").toLowerCase() : "",
    id: el.id || "",
    autocomplete: el.getAttribute ? (el.getAttribute("autocomplete") || "").toLowerCase() : "",
    ariaLabel: el.getAttribute ? (el.getAttribute("aria-label") || "") : "",
    text: (el.innerText || el.value || "").replace(/\s+/g, " ").trim().slice(0, 120),
    valueLength: "value" in el ? (el.value || "").length : null,
    visible: isVisible(el),
    rect: rectSummary(el),
    selector: selectorSummary(el),
    xpath: xpathFor(el)
  };
})()
"""


class CDPClient:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.ws.settimeout(30)
        self.next_id = 0

    def close(self):
        self.ws.close()

    def send(self, method, params=None):
        self.next_id += 1
        message_id = self.next_id
        payload = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        self.ws.send(json.dumps(payload))
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") != message_id:
                continue
            if "error" in data:
                raise RuntimeError(f"CDP {method} failed: {data['error']}")
            return data.get("result", {})

    def evaluate(self, expression, await_promise=False):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if result.get("exceptionDetails"):
            text = result["exceptionDetails"].get("text") or "JavaScript evaluation failed"
            raise RuntimeError(text)
        return (result.get("result") or {}).get("value")


def fetch_json(path):
    url = f"{DEBUG_BASE}{path}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def load_targets():
    last_error = None
    for path in TARGETS_PATHS:
        try:
            return fetch_json(path)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Unable to read DevTools targets from {DEBUG_BASE}: {last_error}")


def is_login_page(target):
    if target.get("type") != "page":
        return False
    url = str(target.get("url") or "")
    if not url or url == "about:blank":
        return False
    return LOGIN_URL_HINT in url


def select_target(targets):
    candidates = [target for target in targets if is_login_page(target)]
    if not candidates:
        raise SystemExit("No top-level LinkedIn login page tab found on the DevTools endpoint.")
    return candidates[0]


def try_activate_target(target_id):
    quoted = urllib.parse.quote(target_id, safe="")
    url = f"{DEBUG_BASE}/json/activate/{quoted}"
    try:
        with urllib.request.urlopen(url, timeout=5):
            return
    except urllib.error.URLError:
        return


def wait_for_ready(client, timeout_seconds=10.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        ready_state = client.evaluate("document.readyState")
        if ready_state == "complete":
            return
        time.sleep(0.25)


def pretty_print(label, value):
    print(f"\n{label}:")
    print(json.dumps(value, indent=2, sort_keys=True))


def mouse_click(client, x, y):
    client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})
    client.send(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    client.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )


def key_event(client, event_type, key, code=None, windows_key_code=None, modifiers=0, text=None):
    params = {
        "type": event_type,
        "key": key,
        "modifiers": modifiers,
    }
    if code is not None:
        params["code"] = code
    if windows_key_code is not None:
        params["windowsVirtualKeyCode"] = windows_key_code
        params["nativeVirtualKeyCode"] = windows_key_code
    if text is not None:
        params["text"] = text
        params["unmodifiedText"] = text
    client.send("Input.dispatchKeyEvent", params)


def select_all_and_clear(client):
    key_event(client, "rawKeyDown", "Control", code="ControlLeft", windows_key_code=17)
    key_event(client, "rawKeyDown", "a", code="KeyA", windows_key_code=65, modifiers=2)
    key_event(client, "keyUp", "a", code="KeyA", windows_key_code=65, modifiers=2)
    key_event(client, "keyUp", "Control", code="ControlLeft", windows_key_code=17)
    key_event(client, "rawKeyDown", "Backspace", code="Backspace", windows_key_code=8)
    key_event(client, "keyUp", "Backspace", code="Backspace", windows_key_code=8)


def type_text_via_keys(client, text):
    for char in text:
        key_event(client, "char", char, text=char)


def active_element_summary(client):
    return client.evaluate(ACTIVE_ELEMENT_SUMMARY_JS)


def element_focus_expression(xpath):
    return f"""
(() => {{
  const xpath = {json.dumps(xpath)};
  const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
  const el = result.singleNodeValue;
  if (!el) {{
    return {{ found: false }};
  }}
  if (el.scrollIntoView) {{
    el.scrollIntoView({{ block: "center", inline: "center" }});
  }}
  if (el.focus) {{
    el.focus({{ preventScroll: true }});
  }}
  return {{
    found: true,
    activeXPath: document.activeElement ? (function(active) {{
      const parts = [];
      let node = active;
      while (node && node.nodeType === Node.ELEMENT_NODE) {{
        let index = 1;
        let sibling = node.previousElementSibling;
        while (sibling) {{
          if (sibling.tagName === node.tagName) index += 1;
          sibling = sibling.previousElementSibling;
        }}
        parts.unshift(`${{node.tagName.toLowerCase()}}[${{index}}]`);
        node = node.parentElement;
      }}
      return "/" + parts.join("/");
    }})(document.activeElement) : null
  }};
}})()
"""


def active_input_matches(client, element, expected_text=None):
    xpath = element.get("xpath")
    expression = f"""
(() => {{
  const xpath = {json.dumps(xpath)};
  const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
  const target = result.singleNodeValue;
  const active = document.activeElement;
  if (!target || !active) {{
    return false;
  }}
  if (target !== active) {{
    return false;
  }}
  if ({json.dumps(expected_text)} === null) {{
    return true;
  }}
  return (active.value || "") === {json.dumps(expected_text)};
}})()
"""
    return bool(client.evaluate(expression))


def ensure_focus(client, element, label):
    client.send("Page.bringToFront")
    rect = (element or {}).get("rect") or {}
    x = rect.get("x")
    y = rect.get("y")
    xpath = (element or {}).get("xpath")
    if x is None or y is None or not xpath:
        raise RuntimeError(f"{label} is missing focus metadata.")

    mouse_click(client, x, y)
    time.sleep(0.15)
    summary = active_element_summary(client)
    pretty_print(f"{label} active element after click", summary)
    if active_input_matches(client, element):
        return summary

    focus_result = client.evaluate(element_focus_expression(xpath))
    pretty_print(f"{label} focus() result", focus_result)
    time.sleep(0.1)
    summary = active_element_summary(client)
    pretty_print(f"{label} active element after focus()", summary)
    if active_input_matches(client, element):
        return summary

    mouse_click(client, x, y)
    time.sleep(0.15)
    summary = active_element_summary(client)
    pretty_print(f"{label} active element after retry click", summary)
    if active_input_matches(client, element):
        return summary

    raise RuntimeError(f"{label} did not receive focus. Active element summary: {summary}")


def focus_and_type(client, element, text, label):
    ensure_focus(client, element, label)
    select_all_and_clear(client)
    time.sleep(0.1)
    client.send("Input.insertText", {"text": text})
    time.sleep(0.15)

    summary = active_element_summary(client)
    pretty_print(f"{label} active element after Input.insertText", summary)
    if not active_input_matches(client, element, text):
        select_all_and_clear(client)
        time.sleep(0.1)
        type_text_via_keys(client, text)
        time.sleep(0.2)
        summary = active_element_summary(client)
        pretty_print(f"{label} active element after key fallback", summary)

    if not active_input_matches(client, element, text):
        raise RuntimeError(f"{label} did not accept native input. Active element summary: {summary}")

    return summary


def collect_post_submit_state(client, baseline_href):
    deadline = time.time() + SETTLE_TIMEOUT_SECONDS
    latest_state = None
    while time.time() < deadline:
        latest_state = client.evaluate(PAGE_STATE_JS)
        if latest_state and latest_state.get("href") != baseline_href:
            break
        time.sleep(0.5)
    return latest_state or client.evaluate(PAGE_STATE_JS)


def validate_selection(chosen):
    text_input = chosen.get("textInput")
    password_input = chosen.get("passwordInput")
    sign_in_button = chosen.get("signInButton")
    if not text_input:
        raise SystemExit("No eligible visible email/phone input found on the selected LinkedIn login page.")
    if not password_input:
        raise SystemExit("No eligible visible password input found on the selected LinkedIn login page.")
    if not sign_in_button:
        raise SystemExit("No eligible visible Sign in button found on the selected LinkedIn login page.")
    if (sign_in_button.get("text") or "").strip() != "Sign in":
        raise SystemExit("Chosen button is not the exact Sign in button, refusing to continue.")
    if sign_in_button["rect"]["y"] < password_input["rect"]["y"]:
        print("\nWarning: chosen Sign in button is not below the password field.")


def main():
    targets = load_targets()
    target = select_target(targets)

    print("Selected target tab:")
    print(f"  id:    {target.get('id')}")
    print(f"  url:   {target.get('url')}")
    print(f"  title: {target.get('title')}")

    email = input("LinkedIn email: ").strip()
    password = getpass("LinkedIn password: ").strip()
    if not email or not password:
        raise SystemExit("Both LinkedIn email and password are required.")

    try_activate_target(str(target.get("id") or ""))

    client = CDPClient(target["webSocketDebuggerUrl"])
    try:
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("DOM.enable")
        client.send("Page.bringToFront")
        wait_for_ready(client)

        controls = client.evaluate(CONTROL_CANDIDATES_JS)
        pretty_print("All visible candidate controls", controls.get("candidates"))
        pretty_print("Chosen selectors / element summary", controls.get("chosen"))

        chosen = controls.get("chosen") or {}
        validate_selection(chosen)

        pre_state = client.evaluate(PAGE_STATE_JS)
        pretty_print("Pre-login page state", pre_state)

        email_state = focus_and_type(client, chosen["textInput"], email, "Email input")
        password_state = focus_and_type(client, chosen["passwordInput"], password, "Password input")
        pretty_print(
            "Input state after typing",
            {
                "emailInput": email_state,
                "passwordInput": password_state,
            },
        )

        client.send("Page.bringToFront")
        button_rect = chosen["signInButton"]["rect"]
        mouse_click(client, button_rect["x"], button_rect["y"])

        post_state = collect_post_submit_state(client, pre_state.get("href"))
        pretty_print("Post-login page state", post_state)

        body_text = client.evaluate(BODY_TEXT_JS) or ""
        print("\nVisible body text after submit (first 800 chars):")
        print(body_text[:800])
    finally:
        client.close()


if __name__ == "__main__":
    main()

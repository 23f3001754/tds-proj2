# app.py
import os
import json
import threading
import time
import re
import io
import logging
from urllib.parse import urljoin, urlparse

from flask import Flask, request, jsonify
import requests
from requests.exceptions import RequestException

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# Optional PDF helper libs
import pdfplumber
from dotenv import load_dotenv
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
QUIZ_SECRET = os.getenv("QUIZ_SECRET")
# --- CONFIG: required env var (fail-fast) ---
SECRET_VALUE = os.environ.get("QUIZ_SECRET")
if not SECRET_VALUE:
    raise RuntimeError("QUIZ_SECRET env var is required. Set it in your deployment config.")

SOLVE_DEADLINE_SECONDS = int(os.environ.get("SOLVE_DEADLINE_SECONDS", "170"))  # must be <180
PER_QUESTION_RETRIES = int(os.environ.get("PER_QUESTION_RETRIES", "2"))
SUBMIT_TIMEOUT = int(os.environ.get("SUBMIT_TIMEOUT", "25"))
PAGE_RENDER_WAIT = float(os.environ.get("PAGE_RENDER_WAIT", "0.8"))
PLAYWRIGHT_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "30000"))

app = Flask(__name__)

# ---------- Helpers ----------
def is_localhost_url(url: str) -> bool:
    try:
        p = urlparse(url)
        hostname = (p.hostname or "").lower()
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        if hostname.endswith(".local"):
            return True
    except Exception:
        return False
    return False

def find_submit_url_from_page(page, base_url):
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href")
        if href and "submit" in href.lower():
            return urljoin(base_url, href)
    text = ""
    try:
        text = page.inner_text("body")
    except Exception:
        pass
    m = re.search(r"https?://[^\s'\"<>]*submit[^\s'\"<>]*", text, flags=re.IGNORECASE)
    if m:
        return m.group(0)
    m2 = re.search(r"https?://[^\s'\"<>]+", text)
    if m2:
        return m2.group(0)
    return None

def extract_json_from_pre(page):
    pre = page.query_selector("pre")
    if not pre:
        return None
    txt = pre.inner_text().strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    try:
        import base64
        decoded = base64.b64decode(txt).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None

# --- PDF helper (used for many quiz types) ---
def download_file_bytes(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

def sum_values_on_pdf_page2(pdf_bytes, column_name="value"):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages) < 2:
            return None
        page = pdf.pages[1]
        try:
            table = page.extract_table()
            if not table:
                return None
            headers = table[0]
            headers_lower = [h.lower() if h else "" for h in headers]
            try:
                idx = headers_lower.index(column_name.lower())
            except ValueError:
                return None
            s = 0.0
            for row in table[1:]:
                try:
                    v = row[idx]
                    if v is None:
                        continue
                    v = v.replace(",", "").strip()
                    s += float(v)
                except Exception:
                    continue
            return s
        except Exception:
            return None

# ---------- Core heuristics (customize these) ----------
def compute_answer_from_page(page, parsed_json):
    """
    Default heuristics:
    - If parsed_json contains 'answer', return it.
    - If parsed_json contains 'file' or 'url' pointing to a PDF, try PDF handler.
    - Otherwise fallback to scanning the visible body for first numeric token.
    Replace/extend this function with robust logic per quiz type.
    """
    # 1) parsed JSON direct
    if isinstance(parsed_json, dict):
        if "answer" in parsed_json:
            return parsed_json["answer"]
        # If parsed JSON points to a file (demo often uses 'url' or 'file')
        if "url" in parsed_json and isinstance(parsed_json["url"], str) and parsed_json["url"].lower().endswith(".pdf"):
            try:
                pdf_bytes = download_file_bytes(parsed_json["url"])
                val = sum_values_on_pdf_page2(pdf_bytes, column_name="value")
                if val is not None:
                    return val
            except Exception as e:
                log.warning("PDF handler failed: %s", e)

    # 2) fallback: check visible page for file links to PDF
    try:
        anchors = page.query_selector_all("a[href]")
        for a in anchors:
            href = a.get_attribute("href")
            if href and href.lower().endswith(".pdf"):
                try:
                    pdf_bytes = download_file_bytes(urljoin(page.url, href))
                    val = sum_values_on_pdf_page2(pdf_bytes, column_name="value")
                    if val is not None:
                        return val
                except Exception as e:
                    log.info("PDF download attempt failed: %s", e)
    except Exception:
        pass

    # 3) fallback: numeric heuristics on visible body
    try:
        body_text = page.inner_text("body", timeout=1000)
    except Exception:
        body_text = ""
    m = re.search(r"([-+]?\d+\.\d+|[-+]?\d+)", body_text)
    if m:
        v = m.group(0)
        if re.fullmatch(r"[-+]?\d+", v):
            return int(v)
        try:
            return float(v)
        except:
            return v
    return "no-answer-found"

def submit_answer_json(submit_url: str, payload: dict, timeout: int = SUBMIT_TIMEOUT):
    resp = requests.post(submit_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"non_json_response": resp.text}

# ---------- Main solver loop ----------
def solve_and_submit_loop(email: str, secret: str, start_url: str):
    start_time = time.time()
    deadline = start_time + SOLVE_DEADLINE_SECONDS
    current_url = start_url
    task_count = 0

    if is_localhost_url(start_url):
        log.error("start_url is local/localhost. Aborting solver loop.")
        return

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(java_script_enabled=True)
            while current_url and time.time() < deadline:
                task_count += 1
                log.info("Starting task #%d: %s", task_count, current_url)
                retries_left = PER_QUESTION_RETRIES
                answered = False

                while time.time() < deadline and retries_left >= 0 and not answered:
                    try:
                        page = context.new_page()
                        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
                        page.goto(current_url)
                        time.sleep(PAGE_RENDER_WAIT)

                        parsed_json = extract_json_from_pre(page)
                        submit_url = find_submit_url_from_page(page, current_url)
                        answer_payload = compute_answer_from_page(page, parsed_json)

                        submit_body = {
                            "email": email,
                            "secret": secret,
                            "url": current_url,
                            "answer": answer_payload
                        }

                        if not submit_url:
                            log.warning("No submit URL discovered on page. Attempting to find candidate next URL in page.")
                            txt = page.inner_text("body")[:3000]
                            m = re.search(r"https?://[^\s'\"<>]+", txt)
                            if m:
                                candidate = m.group(0)
                                if candidate == current_url:
                                    log.warning("candidate equals current_url; aborting this task.")
                                    current_url = None
                                else:
                                    current_url = candidate
                                answered = True
                                page.close()
                                break
                            else:
                                log.error("No submit URL and no candidate next URL; finishing sequence.")
                                current_url = None
                                answered = True
                                page.close()
                                break

                        # Attempt submit
                        log.info("Submitting to %s (retries left %d) answer=%s", submit_url, retries_left, str(answer_payload)[:200])
                        try:
                            resp_json = submit_answer_json(submit_url, submit_body)
                        except RequestException as e:
                            log.error("Submit request failed: %s", e)
                            retries_left -= 1
                            page.close()
                            time.sleep(0.5)
                            continue

                        log.info("Submit response: %s", str(resp_json)[:1000])
                        correct = None
                        next_url = None
                        if isinstance(resp_json, dict):
                            correct = resp_json.get("correct")
                            next_url = resp_json.get("url")

                        if correct is True:
                            log.info("Answer correct for %s", current_url)
                            if next_url:
                                current_url = next_url
                                answered = True
                                page.close()
                                break
                            else:
                                log.info("No next URL provided; sequence complete.")
                                current_url = None
                                answered = True
                                page.close()
                                break
                        else:
                            log.info("Answer not confirmed correct (correct=%s).", str(correct))
                            if next_url:
                                remaining_time = deadline - time.time()
                                if retries_left > 0 and remaining_time > 8:
                                    log.info("Will retry current question (retries left=%d).", retries_left)
                                    retries_left -= 1
                                    page.close()
                                    time.sleep(0.5)
                                    continue
                                else:
                                    log.info("Moving to grader-provided next_url: %s", next_url)
                                    current_url = next_url
                                    answered = True
                                    page.close()
                                    break
                            else:
                                if retries_left > 0 and (deadline - time.time()) > 8:
                                    log.info("Retrying submission (retries left=%d).", retries_left)
                                    retries_left -= 1
                                    page.close()
                                    time.sleep(0.5)
                                    continue
                                else:
                                    log.info("No next URL and retries exhausted or low time; finishing.")
                                    current_url = None
                                    answered = True
                                    page.close()
                                    break

                    except PWTimeoutError as e:
                        log.error("Playwright timeout: %s", e)
                        retries_left -= 1
                        time.sleep(0.5)
                        continue
                    except Exception as e:
                        log.error("Exception while handling page: %s", e)
                        retries_left -= 1
                        time.sleep(0.5)
                        continue

            browser.close()
    except Exception as e:
        log.error("Solver crashed: %s", e)
    finally:
        elapsed = time.time() - start_time
        log.info("Solver loop finished. Elapsed: %.1fs tasks processed: %d", elapsed, task_count)

@app.route("/", methods=["GET"])
def home():
    return "Server is up", 200

# ---------- HTTP endpoints ----------
@app.route("/api/quiz-solver", methods=["POST"])
def solve_quiz():
    try:
        data = request.get_json()
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # Secret verification
    if "secret" not in data or data["secret"] != SECRET_VALUE:
        return jsonify({"error": "Forbidden"}), 403

    if "url" not in data:
        return jsonify({"error": "Missing URL"}), 400

    quiz_url = data["url"]
    answer = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(quiz_url, timeout=30000)  # 30 sec timeout

            html_content = page.content()

            # --- 1. Check if it's a secret task ---
            secret_elem = page.query_selector("#secret")  # adjust selector
            if secret_elem:
                answer = secret_elem.inner_text().strip()

            # --- 2. Check if it's a sum / table task ---
            table_rows = page.query_selector_all("table tr")
            if table_rows:
                numbers = []
                for row in table_rows[1:]:  # skip header
                    cell = row.query_selector("td.value")  # adjust column class/id
                    if cell:
                        val = cell.inner_text().strip()
                        if val.isdigit():
                            numbers.append(int(val))
                        else:
                            try:
                                numbers.append(float(val))
                            except:
                                pass
                if numbers:
                    answer = sum(numbers)

            # --- 3. Fallback / text answer ---
            if answer is None:
                # Try to read from pre tag containing JSON
                pre = page.query_selector("pre")
                if pre:
                    try:
                        json_data = json.loads(pre.inner_text())
                        answer = json_data.get("answer", "unknown")
                    except:
                        pass

            browser.close()

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Return answer to grader
    return jsonify({
        "email": data.get("email"),
        "secret": SECRET_VALUE,
        "url": quiz_url,
        "answer": answer
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # dev use only; in production use gunicorn
    app.run(host="0.0.0.0", port=port)

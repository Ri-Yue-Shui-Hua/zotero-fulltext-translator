#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zotero Fulltext Translator - 版式重建引擎
=========================================
读取"可搜索文字 PDF"，逐页提取文字块(含坐标/字号/颜色) -> 调用 Ollama 翻译 ->
擦除原文 -> 用中文字体按原位置/原字号重新排版写回，输出新的 PDF。

由 Zotero 插件通过子进程调用：
    python translate_pdf.py --config job.json        # 翻译
    python translate_pdf.py --config job.json --check  # 环境自检

所有对外输出均为「每行一个 JSON」(stdout)，事件类型：
    progress / log / done / error / check

退出码：0 成功；2 失败（error 事件里带 code 与 message）
"""
import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

VERSION = "0.1.0"

try:  # PyMuPDF >= 1.24 推荐的导入名
    import pymupdf as fitz
except Exception:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except Exception:
        fitz = None

# ----------------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------------


def emit(type_, **kw):
    kw["type"] = type_
    sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg):
    emit("log", message=str(msg))


class EngineError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
PH_RE = re.compile(r"\[\[\s*(\d+)\s*\]\]")
WORD_RE = re.compile(r"[!-~\u00a0-\u024f]+|\s+|.", re.S)
MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|CMBSY|CMMIB|MSBM|MSAM|Math|Symbol|STIX|MTMI|MTSY|MTEX|"
    r"MTExtra|Euclid|LMMath|TeX-Math|Mathematica)",
    re.I,
)
MATH_CHARS = set("∑∏∫∂∇√∞≈≠≤≥±×÷∈∉⊂⊆∪∩∧∨¬→←↔⇒⇔∘⊗⊕ℝℕℤℚ∀∃∝∼≃≅≡⊥∥⟨⟩αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ")
LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
NO_START = set("，。、；：！？）】》」』”’．…—·％")
NO_END = set("（【《「『“‘")

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
            },
        }
    },
    "required": ["translations"],
}

SYSTEM_PROMPT = """You are a professional academic translator. Translate scientific paper text from English into {lang}.
Rules:
1. Translate faithfully and fluently in an academic tone. Never summarize, explain, or omit anything.
2. Tokens like [[0]], [[1]] are formula placeholders. Copy them exactly as they are, in a natural position. Never translate, alter, or delete them.
3. Keep citation markers (e.g. [12], (Smith et al., 2020)), numbers, units, URLs, variable names, code and equations unchanged.
4. Keep well-known English abbreviations and model/dataset names (e.g. BERT, ImageNet) in English.
5. Output only the translation. No notes, no quotation marks around it.
{extra}
Document title (for context only): {title}"""


# ----------------------------------------------------------------------------
# Ollama 客户端
# ----------------------------------------------------------------------------


class Ollama:
    def __init__(self, base, model, timeout=300, temperature=0.2, num_ctx=8192):
        base = (base or "http://127.0.0.1:11434").strip().rstrip("/")
        if not re.match(r"^https?://", base):
            base = "http://" + base
        # 用户常把 OpenAI 兼容地址 (…/v1) 填进来；本引擎走 Ollama 原生接口 /api/*，这里自动去掉
        base = re.sub(r"/(v1|api)(/.*)?$", "", base).rstrip("/")
        self.base = base
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.num_ctx = num_ctx

    def _request(self, path, payload=None, timeout=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def version(self):
        return self._request("/api/version", timeout=8).get("version", "?")

    def models(self):
        return [m.get("name", "") for m in self._request("/api/tags", timeout=8).get("models", [])]

    def has_model(self, name=None):
        name = name or self.model
        names = self.models()
        if name in names:
            return True
        if ":" not in name and (name + ":latest") in names:
            return True
        return False

    def chat(self, system, user, schema=None, retries=3):
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,  # qwen3 / qwen3.5 等推理模型：关闭思考以提速
            "keep_alive": "30m",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if schema:
            payload["format"] = schema
        last = None
        for attempt in range(retries):
            try:
                resp = self._request("/api/chat", payload)
                content = (resp.get("message") or {}).get("content", "")
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
                return content
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == 404:
                    raise EngineError(
                        "E_MODEL_MISSING",
                        "Ollama 找不到模型 %s，请先执行：ollama pull %s" % (self.model, self.model),
                    )
                last = "HTTP %s %s" % (e.code, body[:300])
                if e.code < 500:
                    break
            except urllib.error.URLError as e:
                raise EngineError(
                    "E_OLLAMA_DOWN",
                    "无法连接 Ollama（%s）：%s。请确认 Ollama 已启动且地址正确。" % (self.base, e.reason),
                )
            except (TimeoutError, OSError) as e:
                last = "timeout/io: %s" % e
            except Exception as e:  # noqa
                last = str(e)
            time.sleep(1.5 * (attempt + 1))
        raise EngineError("E_OLLAMA_FAIL", "Ollama 调用失败：%s" % last)


# ----------------------------------------------------------------------------
# 翻译器（批量 + 缓存 + 校验 + 单条回退）
# ----------------------------------------------------------------------------


class Translator:
    def __init__(self, client, lang, extra_prompt, title, cache_path, batch_chars):
        self.client = client
        self.lang = lang
        self.system = SYSTEM_PROMPT.format(
            lang=lang,
            extra=("6. Additional user instructions: " + extra_prompt.strip()) if extra_prompt.strip() else "",
            title=title or "(unknown)",
        )
        self.cache_path = cache_path
        self.batch_chars = max(300, int(batch_chars))
        self.cache = {}
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
        self.stats = {"cache_hit": 0, "llm_calls": 0, "fallback_single": 0, "failed": 0}

    def _key(self, text):
        return hashlib.sha1(("%s|%s|%s" % (self.client.model, self.lang, text)).encode("utf-8")).hexdigest()

    def save_cache(self):
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except Exception as e:  # noqa
            log("缓存写入失败：%s" % e)

    # -- 校验与修复 ---------------------------------------------------------
    @staticmethod
    def _clean(s):
        s = (s or "").strip()
        s = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", s).strip()
        return s

    @staticmethod
    def _fix_placeholders(dst, n_formula):
        dst = PH_RE.sub(lambda m: "[[%d]]" % int(m.group(1)) if int(m.group(1)) < n_formula else "", dst)
        present = {int(x) for x in PH_RE.findall(dst)}
        missing = [i for i in range(n_formula) if i not in present]
        if missing:  # 公式丢失 -> 追加到段末，保证不丢信息
            dst = dst.rstrip() + " " + " ".join("[[%d]]" % i for i in missing)
        return dst

    def _valid(self, src, dst):
        if not dst:
            return False
        plain = PH_RE.sub(" ", src)
        n_words = len(re.findall(r"[A-Za-z]{2,}", plain))
        if n_words >= 3 and len(CJK_RE.findall(dst)) < 2:
            return False  # 模型没翻译（原样返回）
        return True

    # -- 对外接口 -----------------------------------------------------------
    def translate(self, items):
        """items: [{'text':..., 'nform':int}] -> [str|None]"""
        out = [None] * len(items)
        todo = []
        for i, it in enumerate(items):
            k = self._key(it["text"])
            if k in self.cache:
                out[i] = self.cache[k]
                self.stats["cache_hit"] += 1
            else:
                todo.append(i)
        batch, size = [], 0
        for i in todo:
            L = len(items[i]["text"])
            if batch and size + L > self.batch_chars:
                self._run_batch(items, batch, out)
                batch, size = [], 0
            batch.append(i)
            size += L
        if batch:
            self._run_batch(items, batch, out)
        self.save_cache()
        return out

    def _store(self, items, i, dst, out):
        dst = self._fix_placeholders(self._clean(dst), items[i]["nform"])
        if self._valid(items[i]["text"], dst):
            out[i] = dst
            self.cache[self._key(items[i]["text"])] = dst
            return True
        return False

    def _run_batch(self, items, idxs, out):
        if len(idxs) > 1:
            payload = [{"id": j, "text": items[i]["text"]} for j, i in enumerate(idxs)]
            user = (
                "Translate the `text` of every item. Return JSON exactly like "
                '{"translations":[{"id":0,"text":"..."}]} and cover every id.\n\n'
                + json.dumps(payload, ensure_ascii=False)
            )
            try:
                self.stats["llm_calls"] += 1
                raw = self.client.chat(self.system, user, schema=SCHEMA)
                data = json.loads(raw)
                for t in data.get("translations", []):
                    j = t.get("id")
                    if isinstance(j, int) and 0 <= j < len(idxs):
                        self._store(items, idxs[j], t.get("text", ""), out)
            except EngineError as e:
                if e.code in ("E_OLLAMA_DOWN", "E_MODEL_MISSING"):
                    raise
                log("批量翻译失败，回退逐条：%s" % e.message)
            except Exception as e:  # JSON 解析错误等
                log("批量结果解析失败，回退逐条：%s" % e)
        # 逐条补齐
        for i in idxs:
            if out[i] is not None:
                continue
            self.stats["fallback_single"] += 1
            ok = False
            for _ in range(2):
                try:
                    self.stats["llm_calls"] += 1
                    raw = self.client.chat(self.system, items[i]["text"])
                    if self._store(items, i, raw, out):
                        ok = True
                        break
                except EngineError as e:
                    if e.code in ("E_OLLAMA_DOWN", "E_MODEL_MISSING"):
                        raise
                    log("单条翻译失败：%s" % e.message)
            if not ok:
                self.stats["failed"] += 1


# ----------------------------------------------------------------------------
# 提取：页面 -> 段落块
# ----------------------------------------------------------------------------


def span_is_math(span):
    t = span["text"]
    if not t.strip():
        return False
    if MATH_FONT_RE.search(span.get("font", "")):
        return True
    if re.search(r"[A-Za-z]{3,}", t):
        return False
    return any(c in MATH_CHARS for c in t)


def rgb(color_int):
    return (((color_int >> 16) & 255) / 255.0, ((color_int >> 8) & 255) / 255.0, (color_int & 255) / 255.0)


def line_text(line):
    return "".join(s["text"] for s in line["spans"])


def line_size(line):
    sizes = [s["size"] for s in line["spans"] if s["text"].strip()]
    return max(sizes) if sizes else 0


SENT_END_RE = re.compile(r"[.!?。！？:;：；”\"')\]]\s*$")


def split_segments(line):
    """把一行按「大水平间隙」切成多个片段。表格各列之间间隙很大，一行会被切成多个单元格；
    正文行不会被切开。返回伪行：{'spans','bbox','dir','tab'}，tab=True 表示来自多片段行(疑似表格单元格)。"""
    spans = [sp for sp in line["spans"] if sp["text"] != ""]
    spans.sort(key=lambda sp: sp["bbox"][0])
    segs, cur, last = [], [], None
    for sp in spans:
        blank = not sp["text"].strip()
        if cur and last is not None and not blank:
            gap = sp["bbox"][0] - last["bbox"][2]
            if gap > max(1.8 * max(sp["size"], last["size"], 1), 10):
                segs.append(cur)
                cur = []
        cur.append(sp)
        if not blank:
            last = sp
    if cur:
        segs.append(cur)
    segs = [sg for sg in segs if "".join(x["text"] for x in sg).strip()]
    out = []
    for sg in segs:
        out.append({
            "spans": sg,
            "bbox": (min(x["bbox"][0] for x in sg), min(x["bbox"][1] for x in sg),
                     max(x["bbox"][2] for x in sg), max(x["bbox"][3] for x in sg)),
            "dir": line.get("dir", (1, 0)),
            "tab": len(segs) > 1,
        })
    return out


def can_merge(prev, l, block_x1):
    """l 是否与 prev 属于同一段落（或同一个换行的单元格）"""
    if l["tab"]:
        return False
    ps = max(line_size(prev), 1)
    gap = l["bbox"][1] - prev["bbox"][3]
    if gap > 0.5 * ps or gap < -0.5 * ps:
        return False  # 行距过大(段间距)
    if l["bbox"][1] < prev["bbox"][1] + 0.5 * ps:
        return False  # 不在 prev 正下方(并排的另一列)
    if abs(line_size(l) - ps) > 1.5:
        return False
    if prev["tab"]:
        return abs(l["bbox"][0] - prev["bbox"][0]) <= 2.0  # 单元格内换行
    ptxt = line_text(prev)
    ends = bool(SENT_END_RE.search(ptxt))
    # 段落结束的两个信号：①上一行以句末标点结尾且明显短于版心；②下一行缩进且上一行以句末标点结尾
    # （右侧参差不齐的文本里，行中途偏短是正常现象，不能当成段落结束）
    if ends and prev["bbox"][2] < block_x1 - 3.0 * ps:
        return False
    if ends and l["bbox"][0] > prev["bbox"][0] + 0.8 * ps:
        return False
    if abs(l["bbox"][0] - prev["bbox"][0]) > 3.0 * ps:
        return False
    return True


def group_lines(lines, block_x1):
    groups, cur, prev = [], [], None
    for l in lines:
        if prev is not None and not can_merge(prev, l, block_x1):
            groups.append(cur)
            cur = []
        cur.append(l)
        prev = l
    if cur:
        groups.append(cur)
    return groups


def build_item(lines, page_no, protect_formulas):
    # 主样式：非公式 span 中字符数最多的
    weight = {}
    for l in lines:
        for s in l["spans"]:
            if s["text"].strip() and not span_is_math(s):
                key = (round(s["size"], 1), s["color"], bool(s["flags"] & 16) or "bold" in s.get("font", "").lower())
                weight[key] = weight.get(key, 0) + len(s["text"].strip())
    if not weight:
        return None
    (size, color, bold), _ = max(weight.items(), key=lambda kv: kv[1])

    formulas = []
    pieces = []
    math_chars = 0
    total_chars = 0
    for l in lines:
        buf = ""
        cur = None  # 正在累积的公式文本

        def commit():
            nonlocal cur, buf
            if cur is None:
                return
            core = cur.strip()
            if core:
                lead = cur[: len(cur) - len(cur.lstrip())]
                trail = cur[len(cur.rstrip()):]
                formulas.append(re.sub(r"\s+", " ", core))
                buf += "%s[[%d]]%s" % (lead, len(formulas) - 1, trail)
            else:
                buf += cur
            cur = None

        for s in l["spans"]:
            t = s["text"]
            for k, v in LIGATURES.items():
                t = t.replace(k, v)
            total_chars += len(t.strip())
            small = s["size"] < size * 0.85 and len(t.strip()) <= 4 and not re.search(r"[A-Za-z]{2,}", t) and t.strip()
            is_m = protect_formulas and (span_is_math(s) or small)
            if is_m:
                math_chars += len(t.strip())
                cur = (cur or "") + t
                continue
            if not t.strip() and cur is not None:
                cur += t
                continue
            commit()
            buf += t
        commit()
        pieces.append(buf.strip())

    # 合并行：处理断词连字符
    joined = ""
    for p in pieces:
        if not p:
            continue
        if joined and re.search(r"[A-Za-z]-$", joined) and p[:1].islower():
            joined = joined[:-1] + p
        else:
            joined = (joined + " " + p) if joined else p
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return None

    x0 = min(l["bbox"][0] for l in lines)
    y0 = min(l["bbox"][1] for l in lines)
    x1 = max(l["bbox"][2] for l in lines)
    y1 = max(l["bbox"][3] for l in lines)
    ys = [l["spans"][0]["origin"][1] for l in lines if l["spans"]]
    pitch = statistics.median([b - a for a, b in zip(ys, ys[1:])]) if len(ys) >= 2 else size * 1.2
    cs = [(l["bbox"][0] + l["bbox"][2]) / 2 for l in lines]
    xs = [l["bbox"][0] for l in lines]
    if len(lines) == 1:
        center_cand = True  # 单行：是否居中由页面级判断（接近页面中线）
    else:  # 多行：各行中心对齐、左边缘却不齐，才是真正的居中文本
        center_cand = max(abs(c - sum(cs) / len(cs)) for c in cs) <= 2.0 and (max(xs) - min(xs)) > 3.0
    return {
        "page": page_no,
        "center_cand": center_cand,
        "rect": fitz.Rect(x0, y0, x1, y1),
        "text": joined,
        "formulas": formulas,
        "size": float(size),
        "color": rgb(color),
        "bold": bool(bold),
        "pitch": float(pitch),
        "nlines": len(lines),
        "math_ratio": (math_chars / total_chars) if total_chars else 0.0,
    }


NUM_SEG_RE = re.compile(r"^[\d\s.,±–\-−+%()/<>=*]+$")


def _is_numeric_text(t):
    t = t.strip()
    return bool(t) and bool(NUM_SEG_RE.match(t)) and any(c.isdigit() for c in t)


def span_text_from_chars(span):
    """rawdict 的 span 只有逐字符信息；按字符间隙补回缺失的空格（有些 PDF 的词间距没有空格字符）。"""
    chars = span.get("chars")
    if chars is None:
        return span.get("text", "")
    size = max(span.get("size", 10), 1)
    out, prev_x1, prev_c = [], None, ""
    for ch in chars:
        c, bb = ch["c"], ch["bbox"]
        if prev_x1 is not None and c != " " and prev_c != " " and bb[0] - prev_x1 > 0.22 * size:
            out.append(" ")
        out.append(c)
        prev_x1, prev_c = bb[2], c
    return "".join(out)


def fix_gap_spaces(pl):
    """同一行里相邻 span 之间若有明显间隙却没有空格，补一个空格（避免 Allexaminationswere 这种粘连）"""
    for p in pl:
        prev = None
        for sp in p["spans"]:
            if prev is not None and sp["text"].strip() and prev["text"].strip():
                gap = sp["bbox"][0] - prev["bbox"][2]
                if gap > 0.12 * max(sp["size"], 1) and not prev["text"].endswith(" ") and not sp["text"].startswith(" "):
                    sp["text"] = " " + sp["text"]
            if sp["text"].strip():
                prev = sp


def _columns_aligned(row_xs, ref_xs):
    """各列左边缘在上下行之间重复对齐 => 真正的表格(列对齐)；拉伸的正文行词位置是随机的，不会对齐。"""
    rest, ref_rest = row_xs[1:], ref_xs[1:]
    if not rest or not ref_rest:
        return False
    hit = sum(1 for x in rest if any(abs(x - y) <= 2.0 for y in ref_rest))
    return hit >= max(1, 0.5 * len(rest))


def _try_merge_row(row, refs):
    """一行被拆成多个短碎片，若整行与上/下相邻的行左右两端齐平(两端对齐的特征)，
    且各列不对齐(不是表格)，则判定为「被拉开的正文行」，合并回一行。"""
    row.sort(key=lambda q: q["bbox"][0])
    if any(len(line_text(q).split()) > 6 for q in row):
        return False  # 碎片太长：更像两栏并排，不是拉伸行
    if any(_is_numeric_text(line_text(q)) for q in row):
        return False  # 含纯数字单元格：是表格
    x0 = min(q["bbox"][0] for q in row)
    x1 = max(q["bbox"][2] for q in row)
    y0 = min(q["bbox"][1] for q in row)
    y1 = max(q["bbox"][3] for q in row)
    size = max(line_size(q) for q in row) or 10
    cy = (y0 + y1) / 2
    xs = [q["bbox"][0] for q in row]
    for r in refs:
        if r.get("row") is row:
            continue
        rb = r["bbox"]
        if abs(rb[0] - x0) <= 2.5 and abs(rb[2] - x1) <= 2.5 and 0 < abs((rb[1] + rb[3]) / 2 - cy) <= 2.6 * size \
                and not _columns_aligned(xs, r["xs"]):
            spans = [sp for q in row for sp in q["spans"]]
            row[0].update({"spans": spans, "bbox": (x0, y0, x1, y1), "tab": False})
            for q in row[1:]:
                q["_drop"] = True
            return True
    return False


def merge_stretched_rows(blocks_pl):
    """逐个文字块处理（不跨块，避免把两栏同一高度的行误并成一行）"""
    for pl in blocks_pl:
        _merge_rows_in_block(pl)


def _merge_rows_in_block(frags):
    n = len(frags)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a = frags[i]["bbox"]
        for j in range(i + 1, n):
            b = frags[j]["bbox"]
            h = min(a[3] - a[1], b[3] - b[1])
            vo = min(a[3], b[3]) - max(a[1], b[1])
            if h > 0 and vo > 0.6 * h and abs(line_size(frags[i]) - line_size(frags[j])) <= 1.5 \
                    and (a[2] <= b[0] + 1 or b[2] <= a[0] + 1):
                parent[find(i)] = find(j)
    comp = {}
    for i in range(n):
        comp.setdefault(find(i), []).append(frags[i])
    rows = [c for c in comp.values() if len(c) >= 2]
    # 参照物：整行，以及其他碎片行的外包框（相邻的两行都被拉伸时，互为参照）
    refs = [{"bbox": c[0]["bbox"], "xs": []} for c in comp.values() if len(c) == 1]
    row_refs = {}
    for row in rows:
        row_refs[id(row)] = {"bbox": (min(q["bbox"][0] for q in row), min(q["bbox"][1] for q in row),
                                      max(q["bbox"][2] for q in row), max(q["bbox"][3] for q in row)),
                             "xs": sorted(q["bbox"][0] for q in row), "row": row}
    refs.extend(row_refs.values())
    for _ in range(4):  # 连续几行都被拉伸时，逐轮合并
        left = []
        for row in rows:
            if _try_merge_row(row, refs):
                row_refs[id(row)]["xs"] = []  # 已合并为整行，不再带列信息
            else:
                left.append(row)
        if len(left) == len(rows):
            break
        rows = left


def _overlap_ratio(a, b):
    inter = a & b
    if inter.is_empty:
        return 0.0
    m = min(a.width * a.height, b.width * b.height)
    return (inter.width * inter.height) / m if m > 0 else 0.0


def dedupe_items(items):
    """同一块文字被提取了多次（叠印、隐藏的 OCR 文字层等）时，只保留文字更长的那份；
    被去掉的那份只擦除、不重画，避免同一段出现两个译文互相叠压。"""
    for i, it in enumerate(items):
        it["_i"] = i
    keep, dups = [], []
    for it in sorted(items, key=lambda x: -len(x["text"])):
        dup = None
        if len(it["text"]) >= 40:
            for k in keep:
                if len(k["text"]) >= 40 and _overlap_ratio(it["rect"], k["rect"]) > 0.4:
                    dup = k
                    break
        if dup is None:
            keep.append(it)
        else:
            it["dup_of"] = id(dup)
            dups.append(it)
    keep.sort(key=lambda x: x["_i"])
    return keep, dups


def extract_page(page, page_no, opts):
    """返回 (items, obstacles, dups)。obstacles：图片块、旋转文字、纯公式段等「不翻译但占位置」的区域。"""
    flags = getattr(fitz, "TEXT_MEDIABOX_CLIP", 0)
    d = page.get_text("rawdict", flags=flags)
    obstacles, items = [], []
    block_lines = []  # [(block, [pseudo-lines])]
    for b in d.get("blocks", []):
        lines = b.get("lines", [])
        if b.get("type") != 0 or not lines:
            obstacles.append(fitz.Rect(b["bbox"]))
            continue
        if any(abs(l.get("dir", (1, 0))[0]) < 0.99 for l in lines):  # 旋转文字(arXiv 水印等)
            obstacles.append(fitz.Rect(b["bbox"]))
            continue
        for l in lines:
            for sp in l["spans"]:
                sp["text"] = span_text_from_chars(sp)
        pl = []
        for l in lines:
            if line_text(l).strip():
                pl.extend(split_segments(l))
        if pl:
            block_lines.append((b, pl))

    merge_stretched_rows([pl for _b, pl in block_lines])

    for b, pl in block_lines:
        pl = [p for p in pl if not p.get("_drop")]
        if not pl:
            continue
        fix_gap_spaces(pl)
        flow = [l["bbox"][2] for l in pl if not l["tab"]]
        bx1 = max(flow) if flow else max(l["bbox"][2] for l in pl)
        for g in group_lines(pl, bx1):
            it = build_item(g, page_no, opts["protect_formulas"])
            if it:
                it["tab"] = bool(g[0]["tab"])
                items.append(it)
            else:  # 纯公式/符号段：不翻译，但要当障碍物
                obstacles.append(fitz.Rect(min(l["bbox"][0] for l in g), min(l["bbox"][1] for l in g),
                                           max(l["bbox"][2] for l in g), max(l["bbox"][3] for l in g)))
    mark_row_neighbors(items, obstacles)
    items, dups = dedupe_items(items)
    return items, obstacles, dups


def mark_row_neighbors(items, obstacles):
    """PyMuPDF 常把表格的每个单元格拆成独立的行/块。凡是「短(≤2行)、且在同一水平带左右各有另一个短内容」
    的段落，视为表格单元格(tab)：不与上下文合并、可向右借空白、短文本也翻译。
    邻居也必须是短内容，这样两栏论文里的小标题不会因为隔壁栏有长段落而被误判。"""
    shorts = [it for it in items if it["nlines"] <= 2]
    short_obs = [ob for ob in obstacles if ob.height < 2.5 * 12]
    for it in shorts:
        if it["tab"]:
            continue
        r = it["rect"]
        for other in [o["rect"] for o in shorts if o is not it] + short_obs:
            vo = min(r.y1, other.y1) - max(r.y0, other.y0)
            if vo > 0.5 * min(r.height, other.height) and (other.x0 >= r.x1 + 1.5 * it["size"] or other.x1 <= r.x0 - 1.5 * it["size"]):
                it["tab"] = True
                break


REF_HEAD = re.compile(r"^(\d+\.?\s*|[IVX]+\.?\s*)?(references|bibliography|参考文献)\s*$", re.I)
APPX_HEAD = re.compile(r"^(appendix|appendices|supplementary (material|information))", re.I)


def should_translate(it, page, opts, state):
    plain = PH_RE.sub(" ", it["text"]).strip()
    if REF_HEAD.match(plain) and it["nlines"] <= 2:
        state["in_refs"] = True
        return False, "refs-heading"
    if APPX_HEAD.match(plain) and it["nlines"] <= 2:
        state["in_refs"] = False
    if state["in_refs"] and opts["skip_references"]:
        return False, "refs"
    if len(CJK_RE.findall(plain)) > len(plain) * 0.3:
        return False, "cjk"
    ph = page.rect.height
    r = it["rect"]
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]+", plain)
    if opts["skip_header_footer"] and (r.y1 < ph * 0.07 or r.y0 > ph * 0.93) and len(words) < 14:
        return False, "header/footer"
    if it.get("tab"):  # 表格单元格：短文本也要翻译（表头、行标签），纯数字/全大写缩写保持原样
        if not opts["translate_table_cells"]:
            return False, "table"
        cand = [w for w in words if len(w) >= 3 and not w.isupper()]
        if not cand or len(words) > 15:
            return False, "table-short"
    elif len(words) < 3 and not (len(words) >= 1 and it["bold"] and len(plain) >= 4):
        return False, "too-short"
    if " " not in plain and len(plain) > 25:
        return False, "url/token"
    if it["math_ratio"] > 0.7:
        return False, "formula-block"
    if not opts["translate_captions"] and re.match(r"^(Fig(ure)?\.?|Table)\s*\d+", plain, re.I):
        return False, "caption"
    return True, ""


# ----------------------------------------------------------------------------
# 排版：中文字体、换行、缩放适配
# ----------------------------------------------------------------------------

FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    # Linux（优先简体中文专用 OTF；.ttc 合集默认取第 0 个字面，可能是日文字形）
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]


SC_FACE_RE = re.compile(r"\bSC\b|Simplified|Hans|\bGB\b|YaHei|雅黑|SimSun|宋体|SimHei|黑体|Songti SC|PingFang SC|STHeiti|Heiti SC", re.I)


def resolve_font_file(path):
    """.ttc 合集：抽出简体中文字面为独立字体文件（缓存），这样 ①字形是简体 ②subset_fonts 才能瘦身。
    需要 fonttools；缺失或失败时原样返回。"""
    if not path or not path.lower().endswith(".ttc"):
        return path
    try:
        from fontTools.ttLib import TTCollection
        st = os.stat(path)
        key = hashlib.sha1(("%s|%s|%s" % (path, st.st_size, st.st_mtime_ns)).encode("utf-8")).hexdigest()[:16]
        cache_dir = os.path.join(tempfile.gettempdir(), "zotero-ftt-fonts")
        os.makedirs(cache_dir, exist_ok=True)
        for ext in (".otf", ".ttf"):
            cached = os.path.join(cache_dir, key + ext)
            if os.path.exists(cached):
                return cached
        ttc = TTCollection(path)
        pick = 0
        for i, face in enumerate(ttc.fonts):
            nm = face["name"]
            label = " ".join(filter(None, [nm.getDebugName(1), nm.getDebugName(4), nm.getDebugName(16)]))
            if SC_FACE_RE.search(label):
                pick = i
                break
        face = ttc.fonts[pick]
        out = os.path.join(cache_dir, key + (".otf" if "CFF " in face else ".ttf"))
        face.save(out)
        log("已从字体合集抽取第 %d 个字面：%s" % (pick, os.path.basename(path)))
        return out
    except Exception as e:  # noqa
        log("字体合集抽取失败，直接使用原文件（建议安装 fonttools 或改选 .ttf/.otf）：%s" % e)
        return path


def load_cjk_font(path):
    """返回 (Font, 说明, 实际字体文件路径|None)。优先用户路径，其次系统常见字体，最后 PyMuPDF 内置。"""
    tried = []
    for p in ([path] if path else []) + ([] if path else FONT_CANDIDATES):
        if not p:
            continue
        if not os.path.exists(p):
            tried.append(p)
            continue
        try:
            rp = resolve_font_file(p)
            f = fitz.Font(fontfile=rp)
            if f.has_glyph(ord("中")):
                return f, p, rp
            tried.append(p + "(缺少中文字形)")
        except Exception as e:  # noqa
            tried.append("%s(%s)" % (p, e))
    if path:
        raise EngineError("E_FONT", "中文字体无法加载：%s。请在设置中选择 .ttf/.otf 中文字体。" % path)
    for name in ("cjk", "china-s"):
        try:
            f = fitz.Font(name)
            return f, "builtin:" + name, None
        except Exception:
            continue
    raise EngineError("E_FONT", "未找到可用中文字体，请在设置中指定字体文件（如 NotoSansSC-Regular.otf）。")


def subset_font_file(font_path, chars):
    """用 fontTools 把字体裁剪到只含用到的字符（几百 KB），返回新字体路径；失败返回 None。"""
    try:
        from fontTools import subset
        opts = subset.Options()
        opts.layout_features = []
        opts.name_IDs = [1, 2, 4, 6]
        opts.notdef_outline = True
        opts.hinting = False
        opts.glyph_names = False
        opts.retain_gids = True  # CID 键控 CFF(Noto CJK 等)必须保留 GID，否则 PDF 里的字符会错位
        opts.legacy_kern = False
        font = subset.load_font(font_path, opts)
        sub = subset.Subsetter(opts)
        sub.populate(text="".join(sorted(chars)))
        sub.subset(font)
        key = hashlib.sha1(("".join(sorted(chars)) + font_path).encode("utf-8")).hexdigest()[:12]
        out = os.path.join(tempfile.gettempdir(), "zotero-ftt-fonts", "sub-%s%s" % (key, os.path.splitext(font_path)[1] or ".otf"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        subset.save_font(font, out, opts)
        font.close()
        return out
    except Exception as e:  # noqa
        log("字体裁剪跳过（安装 fonttools 可使输出文件小很多）：%s" % e)
        return None


class Typesetter:
    def __init__(self, font, bold_font, latin_font):
        self.font = font
        self.bold_font = bold_font or font
        self.latin = latin_font

    def _formula_font(self, text):
        if all(self.font.has_glyph(ord(c)) for c in text if not c.isspace()):
            return self.font
        try:
            if all(self.latin.has_glyph(ord(c)) for c in text if not c.isspace()):
                return self.latin
        except Exception:
            pass
        return self.font

    def tokenize(self, text, formulas, bold):
        base = self.bold_font if bold else self.font
        toks = []

        def plain(seg):
            for m in WORD_RE.finditer(seg):
                s = m.group()
                if s.isspace():
                    toks.append(("s", " ", base))
                else:
                    toks.append(("w", s, base))

        pos = 0
        for m in PH_RE.finditer(text):
            plain(text[pos: m.start()])
            idx = int(m.group(1))
            ft = formulas[idx] if idx < len(formulas) else ""
            if ft:
                toks.append(("f", ft, self._formula_font(ft)))
            pos = m.end()
        plain(text[pos:])
        return toks

    @staticmethod
    def _tw(tok, fs):
        return tok[2].text_length(tok[1], fontsize=fs)

    def wrap(self, toks, width, fs):
        lines = []
        cur, curw = [], 0.0

        def strip_trailing():
            nonlocal curw
            while cur and cur[-1][0][0] == "s":
                curw -= cur[-1][1]
                cur.pop()

        # 超宽的长 token（URL 等）按字符切开
        expanded = []
        for t in toks:
            if t[0] != "s" and self._tw(t, fs) > width and len(t[1]) > 1:
                chunk = ""
                for ch in t[1]:
                    if chunk and self._tw((t[0], chunk + ch, t[2]), fs) > width:
                        expanded.append((t[0], chunk, t[2]))
                        chunk = ch
                    else:
                        chunk += ch
                if chunk:
                    expanded.append((t[0], chunk, t[2]))
            else:
                expanded.append(t)

        for t in expanded:
            if t[0] == "s":
                if not cur:
                    continue
                w = self._tw(t, fs)
                cur.append((t, w))
                curw += w
                continue
            w = self._tw(t, fs)
            if cur and curw + w > width + 0.01 and t[1] not in NO_START:
                strip_trailing()
                carry = []
                if len(cur) > 1 and cur[-1][0][1] in NO_END:
                    carry = [cur.pop()]
                    curw -= carry[0][1]
                lines.append((cur, curw))
                cur = carry
                curw = sum(x[1] for x in cur)
            cur.append((t, w))
            curw += w
        strip_trailing()
        if cur:
            lines.append((cur, curw))
        return lines

    def draw(self, page, rect, lines, fs, ratio, color, align):
        """用 TextWriter 写字（正确处理 CJK 的 Identity-H 编码）。
        字体已被裁剪成只含用到的字符，所以每个段落各嵌一份也只有几十 KB，且相同对象会被 garbage=4 合并。"""
        tw = fitz.TextWriter(page.rect)
        y = rect.y0 + fs * 0.9
        for line, lw in lines:
            x = rect.x0 + ((rect.width - lw) / 2.0 if align == "center" else 0.0)
            cx, run, rf, rx = x, [], None, x
            for t, w in line:
                if rf is None:
                    rf, rx = t[2], cx
                elif t[2] is not rf:
                    tw.append(fitz.Point(rx, y), "".join(run), font=rf, fontsize=fs)
                    run, rf, rx = [], t[2], cx
                run.append(t[1])
                cx += w
            if run:
                tw.append(fitz.Point(rx, y), "".join(run), font=rf, fontsize=fs)
            y += fs * ratio
        tw.write_text(page, color=color)


# ----------------------------------------------------------------------------
# 背景色采样 + 擦除
# ----------------------------------------------------------------------------


def sample_bg(pix, rect):
    pad = 1.5
    mx, my = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    pts = [
        (rect.x0 - pad, rect.y0 - pad), (rect.x1 + pad, rect.y0 - pad),
        (rect.x0 - pad, rect.y1 + pad), (rect.x1 + pad, rect.y1 + pad),
        (mx, rect.y0 - pad), (mx, rect.y1 + pad),
        (rect.x0 - pad, my), (rect.x1 + pad, my),
    ]
    vals = []
    for x, y in pts:
        ix = min(max(int(x), 0), pix.width - 1)
        iy = min(max(int(y), 0), pix.height - 1)
        vals.append(pix.pixel(ix, iy)[:3])
    ch = [statistics.median(v[i] for v in vals) for i in range(3)]
    if all(c >= 245 for c in ch):
        return (1, 1, 1)
    return tuple(c / 255.0 for c in ch)


def apply_redactions(page):
    kwargs = {}
    if hasattr(fitz, "PDF_REDACT_IMAGE_NONE"):
        kwargs["images"] = fitz.PDF_REDACT_IMAGE_NONE
    if hasattr(fitz, "PDF_REDACT_LINE_ART_NONE"):
        kwargs["graphics"] = fitz.PDF_REDACT_LINE_ART_NONE
    try:
        page.apply_redactions(**kwargs)
    except TypeError:
        page.apply_redactions()


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------


def parse_range(spec, n):
    spec = (spec or "").strip()
    if not spec:
        return set(range(n))
    out = set()
    for part in re.split(r"[,\s;，]+", spec):
        if not part:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            raise EngineError("E_CONFIG", "页码范围格式错误：%s（示例：1-3,5）" % part)
        a = int(m.group(1))
        b = int(m.group(2) or a)
        out.update(range(max(a, 1) - 1, min(b, n)))
    return out


def normalize_opts(cfg):
    o = {
        "input": cfg.get("input"),
        "output": cfg.get("output"),
        "cache": cfg.get("cache") or "",
        "ollama_url": cfg.get("ollama_url", "http://127.0.0.1:11434"),
        "model": cfg.get("model", "qwen3.5:4b"),
        "font": cfg.get("font") or "",
        "bold_font": cfg.get("bold_font") or "",
        "mode": cfg.get("mode", "replace"),  # replace | bilingual
        "bg_fill": cfg.get("bg_fill", "auto"),  # auto | white | none
        "min_font_scale": max(0.4, min(1.0, float(cfg.get("min_font_scale", 0.6)))),
        "skip_references": bool(cfg.get("skip_references", True)),
        "skip_header_footer": bool(cfg.get("skip_header_footer", True)),
        "protect_formulas": bool(cfg.get("protect_formulas", True)),
        "translate_captions": bool(cfg.get("translate_captions", True)),
        "translate_table_cells": bool(cfg.get("translate_table_cells", True)),
        "unify_font": bool(cfg.get("unify_font", True)),
        "debug": bool(cfg.get("debug", False)),
        "page_range": cfg.get("page_range", ""),
        "extra_prompt": cfg.get("extra_prompt", ""),
        "target_lang": cfg.get("target_lang", "简体中文"),
        "batch_chars": int(cfg.get("batch_chars", 1800)),
        "timeout": int(cfg.get("timeout", 300)),
        "temperature": float(cfg.get("temperature", 0.2)),
        "num_ctx": int(cfg.get("num_ctx", 8192)),
    }
    return o


def run_check(cfg):
    o = normalize_opts(cfg)
    items = []

    def add(name, ok, detail):
        items.append({"name": name, "ok": bool(ok), "detail": detail})

    add("Python", True, sys.version.split()[0])
    if fitz is None:
        add("PyMuPDF", False, "未安装。请执行：pip install pymupdf fonttools")
    else:
        add("PyMuPDF", True, getattr(fitz, "__version__", getattr(fitz, "VersionBind", "?")))
        try:
            f, src, _rp = load_cjk_font(o["font"])
            add("中文字体", True, src)
        except EngineError as e:
            add("中文字体", False, e.message)
    try:
        import fontTools  # noqa
        add("fonttools(字体子集化，减小体积)", True, "已安装")
    except Exception:
        add("fonttools(字体子集化，减小体积)", False, "未安装（可选，但强烈建议）：pip install fonttools")
    cli = Ollama(o["ollama_url"], o["model"])
    try:
        v = cli.version()
        add("Ollama 服务", True, "%s  v%s" % (cli.base, v))
        try:
            ok = cli.has_model()
            add("模型 " + o["model"], ok, "已就绪" if ok else "未找到，请执行：ollama pull " + o["model"])
        except Exception as e:  # noqa
            add("模型 " + o["model"], False, str(e))
    except Exception as e:  # noqa
        add("Ollama 服务", False, "无法连接 %s：%s" % (cli.base, e))
    emit("check", ok=all(i["ok"] for i in items if "可选" not in i["detail"]), items=items)
    return 0


def cap_oversize(items):
    """字号明显大于自身行距(>1.05 倍)说明字号取值异常，按行距回推。"""
    for it in items:
        if not it["tab"] and it["nlines"] >= 2 and it["pitch"] > 0 and it["size"] > it["pitch"] / 1.05:
            it["size"] = it["pitch"] / 1.25


def unify_sizes(all_items, done_items):
    """统一正文字号：取全文按字符数加权的主导字号为正文字号；
    与它相差 ≤10% 的段落、以及 ≥3 行的长段落（无论原字号多大）都按正文字号排。标题/小号脚注保持原比例。"""
    wt = {}
    for it in all_items:
        if it["tab"] or it["bold"] or it["nlines"] < 2 or len(it["text"]) < 40:
            continue
        k = round(it["size"] * 2) / 2
        wt[k] = wt.get(k, 0) + len(it["text"])
    if not wt:
        return None
    body = max(wt.items(), key=lambda kv: kv[1])[0]
    for it in done_items:
        if it["tab"]:
            continue
        long_para = it["nlines"] >= 3 and len(it["text"]) >= 150 and not it["bold"]
        if abs(it["size"] - body) / body <= 0.10 or long_para:
            it["size"] = body
            it["pitch"] = body * 1.3
    return body


def _xo(a, b):
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def build_cands(min_scale):
    cands = [(1.0, False), (1.0, True)]
    n = int((1.0 - min_scale) / 0.04)
    cands += [(round(1.0 - 0.04 * i, 2), True) for i in range(1, n + 1)]
    if cands[-1][0] > min_scale + 1e-6:
        cands.append((min_scale, True))
    return cands


def fit_stack(ts, stack, limit, cands, start=0):
    """同一栏连续的一组段落一起排：前一段变长就把后一段往下推；放不下就整体缩小。
    start：从第几档（scale, tight）开始尝试——用来让整份文档的正文使用同一档。"""
    res = []
    for idx in range(start, len(cands)):
        scale, tight = cands[idx]
        res, cursor = [], -1e9
        for e in stack:
            fs = max(4.5, e["size"] * scale)
            ratio = 1.15 if tight else min(max(e["ratio0"], 1.15), 1.4)
            lines = ts.wrap(e["toks"], e["width"], fs)
            h = (len(lines) - 1) * fs * ratio + fs * 1.15
            y0 = max(e["rect"].y0, cursor)
            res.append((e, y0, fs, ratio, lines, h))
            cursor = y0 + h + 0.1 * fs
        last = res[-1]
        if last[1] + last[5] <= limit + 0.5:
            return res, True, idx
    return res, False, len(cands) - 1


def prepare_layout(page, done, all_items, extra_obs, ts):
    """把一页上译好的段落连成「栈」(同栏上下相邻)，并算出每个栈向下可用的极限位置。返回 [(stack, limit)]"""
    page_w, page_h = page.rect.width, page.rect.height
    obst = list(extra_obs) + [it["rect"] for it in all_items]
    entries = []
    for it, dst in done:
        r = it["rect"]
        width = r.width * 1.02 + 1.0
        if it["tab"]:  # 表格单元格：可向右借用到同一行下一个内容之前的空白
            right = [ob.x0 for ob in obst if ob is not r and ob.x0 >= r.x1 - 1.0
                     and min(ob.y1, r.y1) - max(ob.y0, r.y0) > 0.3 * r.height]
            lim = (min(right) - 4.0) if right else (page_w - 20.0)
            width = max(width, lim - r.x0)
        entries.append({
            "it": it, "rect": r, "size": it["size"], "width": width,
            "toks": ts.tokenize(dst, it["formulas"], it["bold"]),
            "ratio0": it["pitch"] / it["size"] if it["size"] else 1.3,
        })
    entries.sort(key=lambda e: (round(e["rect"].y0, 1), e["rect"].x0))

    stacks = []
    for e in entries:
        placed = False
        if not e["it"]["tab"]:
            for st in reversed(stacks):
                last = st[-1]
                if last["it"]["tab"]:
                    continue
                lr, r = last["rect"], e["rect"]
                if _xo(lr, r) < 0.5 * min(lr.width, r.width):
                    continue
                if r.y0 < lr.y1 - 2 or r.y0 - lr.y1 > 2.0 * max(e["size"], last["size"]):
                    continue
                gx0, gx1 = max(lr.x0, r.x0), min(lr.x1, r.x1)
                if any(ob is not lr and ob is not r and min(ob.x1, gx1) - max(ob.x0, gx0) > 1
                       and min(ob.y1, r.y0) - max(ob.y0, lr.y1) > 0.5 for ob in obst):
                    continue
                st.append(e)
                placed = True
                break
        if not placed:
            stacks.append([e])

    out = []
    for st in stacks:
        own = {id(e["rect"]) for e in st}
        bottom = max(e["rect"].y1 for e in st)
        x0, x1 = min(e["rect"].x0 for e in st), max(e["rect"].x1 for e in st)
        below = [ob.y0 for ob in obst
                 if id(ob) not in own and ob.y0 >= bottom - 1.0
                 and min(ob.x1, x1) - max(ob.x0, x0) > 0.3 * min(ob.width, x1 - x0)]
        out.append((st, (min(below) - 1.0) if below else (page_h - 18)))
    return out


def draw_stack(page, res, page_w, ts):
    for e, y0, fs, ratio, lines, h in res:
        r, it = e["rect"], e["it"]
        if not it["center_cand"] or it["tab"]:
            center = False
        elif it["nlines"] == 1:
            center = abs((r.x0 + r.x1) / 2 - page_w / 2) < page_w * 0.02 and r.width < page_w * 0.85
        else:
            center = True
        ts.draw(page, fitz.Rect(r.x0, y0, r.x0 + e["width"], y0 + h), lines, fs, ratio, it["color"],
                "center" if center else "left")


def translate_pdf(cfg):
    o = normalize_opts(cfg)
    if fitz is None:
        raise EngineError("E_NO_PYMUPDF", "Python 环境缺少 PyMuPDF，请执行：pip install pymupdf fonttools")
    if not o["input"] or not os.path.exists(o["input"]):
        raise EngineError("E_INPUT", "找不到输入 PDF：%s" % o["input"])
    if o["mode"] not in ("replace", "bilingual"):
        raise EngineError("E_CONFIG", "未知翻译模式：%s" % o["mode"])

    try:
        src = fitz.open(o["input"])
    except Exception as e:  # noqa
        raise EngineError("E_PDF_OPEN", "无法打开 PDF：%s" % e)
    if src.needs_pass:
        raise EngineError("E_PDF_ENCRYPTED", "PDF 已加密，需要先解除密码。")
    total = src.page_count
    pages = sorted(parse_range(o["page_range"], total))
    if not pages:
        raise EngineError("E_CONFIG", "页码范围为空。")

    font, font_src, font_path = load_cjk_font(o["font"])
    log("中文字体：%s" % font_src)
    bold_path = None
    if o["bold_font"]:
        if os.path.exists(o["bold_font"]):
            bold_path = resolve_font_file(o["bold_font"])
        else:
            log("粗体字体不存在，改用常规字体：%s" % o["bold_font"])

    client = Ollama(o["ollama_url"], o["model"], o["timeout"], o["temperature"], o["num_ctx"])
    try:
        client.version()
    except Exception as e:  # noqa
        raise EngineError("E_OLLAMA_DOWN", "无法连接 Ollama（%s）：%s" % (client.base, e))
    try:
        if not client.has_model():
            raise EngineError("E_MODEL_MISSING", "Ollama 里没有模型 %s，请先执行：ollama pull %s" % (o["model"], o["model"]))
    except EngineError:
        raise
    except Exception:
        pass

    title = (src.metadata or {}).get("title", "")
    tr = Translator(client, o["target_lang"], o["extra_prompt"], title, o["cache"], o["batch_chars"])

    doc = fitz.open(o["input"])  # 在副本上改
    state = {"in_refs": False}
    summary = {"pages": len(pages), "blocks_translated": 0, "blocks_skipped": 0, "blocks_failed": 0,
               "shrunk": 0, "overflow": 0}
    text_found = 0
    t0 = time.time()

    # ---------- 阶段 1：逐页提取 + 翻译 ----------
    plans = {}  # pdf_page -> (done[(item, dst)], obstacles, items, erase_rects)
    doc_items, dbg_pages = [], []
    for n, pi in enumerate(pages):
        page = doc[pi]
        items, obstacles, dups = extract_page(page, pi, o)
        doc_items.extend(items)
        text_found += len(items)
        todo = []
        for it in items:
            ok, _why = should_translate(it, page, o, state)
            if ok:
                todo.append(it)
            else:
                summary["blocks_skipped"] += 1
        emit("progress", phase="translate", page=n + 1, total=len(pages), pdf_page=pi + 1,
             message="第 %d 页：翻译 %d 个段落" % (pi + 1, len(todo)), elapsed=round(time.time() - t0, 1))
        results = tr.translate([{"text": it["text"], "nform": len(it["formulas"])} for it in todo]) if todo else []
        done = [(it, r) for it, r in zip(todo, results) if r]
        summary["blocks_failed"] += len(todo) - len(done)
        done_ids = {id(it) for it, _ in done}
        erase = [dp["rect"] for dp in dups if dp["dup_of"] in done_ids]
        if o["debug"]:
            dmap = {id(it): dst for it, dst in done}
            dbg_pages.append({"page": pi + 1, "items": [
                {"rect": [round(v, 1) for v in it["rect"]], "size": round(it["size"], 2), "lines": it["nlines"],
                 "tab": it["tab"], "bold": it["bold"], "text": it["text"][:140], "zh": (dmap.get(id(it)) or "")[:140]}
                for it in items], "duplicates_removed": [[round(v, 1) for v in r] for r in erase]})
        if done:
            plans[pi] = (done, obstacles, items, erase)

    if text_found == 0:
        raise EngineError("E_NO_TEXT", "该 PDF 没有可提取的文字层（疑似扫描件）。请先用 OCR 生成可搜索 PDF。")

    # ---------- 字号归一：异常字号纠正 + 统一正文字号 ----------
    cap_oversize(doc_items)
    if o["unify_font"]:
        all_done = [it for done, _o, _i, _e in plans.values() for it, _d in done]
        body = unify_sizes(doc_items, all_done)
        if body:
            log("统一正文字号：%.1f pt" % body)

    # ---------- 字体裁剪：只保留译文用到的字符 ----------
    chars = set(chr(c) for c in range(32, 127)) | set("。，、；：？！（）【】《》“”‘’…—·％×±°")
    for done, _obs, _items, _erase in plans.values():
        for it, dst in done:
            chars |= set(dst)
            for f in it["formulas"]:
                chars |= set(f)
    if font_path:
        sp = subset_font_file(font_path, chars)
        if sp:
            font = fitz.Font(fontfile=sp)
    bold_font = None
    if bold_path:
        try:
            sp = subset_font_file(bold_path, chars) or bold_path
            bold_font = fitz.Font(fontfile=sp)
        except Exception as e:  # noqa
            log("粗体字体加载失败，改用常规字体：%s" % e)
    ts = Typesetter(font, bold_font, fitz.Font("helv"))

    # ---------- 阶段 2：擦除原文 + 重建版式 ----------
    emit("progress", phase="layout", page=len(pages), total=len(pages), message="正在重建版式…",
         elapsed=round(time.time() - t0, 1))
    cands = build_cands(o["min_font_scale"])
    layouts, req = {}, []
    for pi, (done, obstacles, all_items, _erase) in plans.items():  # 第一遍：算出每个栈需要的缩放档
        stacks = prepare_layout(doc[pi], done, all_items, obstacles, ts)
        layouts[pi] = stacks
        for st, limit in stacks:
            if not st[0]["it"]["tab"]:
                req.append(fit_stack(ts, st, limit, cands, 0)[2])
    # 统一缩放：全文正文共用同一档（取第 90 百分位，即 90% 的栏无需更小），个别放不下的栏才单独再缩
    doc_idx = sorted(req)[int(0.9 * (len(req) - 1))] if (req and o["unify_font"]) else 0
    if req and doc_idx:
        log("正文统一缩放：%.0f%%%s" % (cands[doc_idx][0] * 100, "，行距收紧" if cands[doc_idx][1] else ""))

    for pi, (done, obstacles, all_items, erase) in plans.items():  # 第二遍：擦除 + 重画
        page = doc[pi]
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False) if o["bg_fill"] == "auto" else None
        for r in [it["rect"] for it, _ in done] + erase:  # 只擦除「译了的」段落/单元格 + 被判定为重复的那份
            fill = False
            if o["bg_fill"] == "white":
                fill = (1, 1, 1)
            elif pix is not None:
                fill = sample_bg(pix, r)
            page.add_redact_annot(fitz.Rect(r.x0 + 0.3, r.y0 + 0.3, r.x1 - 0.3, r.y1 - 0.3), fill=fill)
        apply_redactions(page)
        for st, limit in layouts[pi]:
            start = doc_idx if (o["unify_font"] and not st[0]["it"]["tab"]) else 0
            res, ok, idx = fit_stack(ts, st, limit, cands, start)
            if idx > 0:
                summary["shrunk"] += len(st)
            if not ok:
                summary["overflow"] += len(st)
            draw_stack(page, res, page.rect.width, ts)
            summary["blocks_translated"] += len(st)

    # ---------- 输出 ----------
    out_path = o["output"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    final = doc
    if o["mode"] == "bilingual":
        final = fitz.open()
        for pi in range(total):
            final.insert_pdf(src, from_page=pi, to_page=pi)
            if pi in pages:
                final.insert_pdf(doc, from_page=pi, to_page=pi)
    tmp = out_path + ".part"
    try:
        final.save(tmp, garbage=4, deflate=True)
    except Exception as e:  # noqa
        raise EngineError("E_SAVE", "保存 PDF 失败：%s" % e)
    os.replace(tmp, out_path)
    tr.save_cache()
    if o["debug"]:
        try:
            with open(out_path + ".debug.json", "w", encoding="utf-8") as f:
                json.dump({"summary": summary, "pages": dbg_pages}, f, ensure_ascii=False, indent=1)
        except Exception as e:  # noqa
            log("诊断文件写入失败：%s" % e)
    summary.update(tr.stats)
    summary["seconds"] = round(time.time() - t0, 1)
    summary["size_mb"] = round(os.path.getsize(out_path) / 1048576, 2)
    emit("done", output=out_path, summary=summary)
    return 0


def split_summary_text(text, max_chars):
    chunks = []
    current = []
    current_size = 0
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        while len(paragraph) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_size = [], 0
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        if paragraph and current_size + len(paragraph) > max_chars and current:
            chunks.append("\n\n".join(current))
            current, current_size = [], 0
        if paragraph:
            current.append(paragraph)
            current_size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def summarize_pdf(cfg):
    o = normalize_opts(cfg)
    if fitz is None:
        raise EngineError("E_NO_PYMUPDF", "Python 环境缺少 PyMuPDF，请执行：pip install pymupdf fonttools")
    if not o["input"] or not os.path.exists(o["input"]):
        raise EngineError("E_INPUT", "找不到输入 PDF：%s" % o["input"])
    try:
        doc = fitz.open(o["input"])
    except Exception as e:  # noqa
        raise EngineError("E_PDF_OPEN", "无法打开 PDF：%s" % e)
    if doc.needs_pass:
        raise EngineError("E_PDF_ENCRYPTED", "PDF 已加密，需要先解除密码。")

    title = (cfg.get("title") or (doc.metadata or {}).get("title") or "未命名论文").strip()
    pages = []
    for page_no, page in enumerate(doc, 1):
        text = page.get_text("text", sort=True).strip()
        if text:
            pages.append("【第 %d 页】\n%s" % (page_no, text))
    doc.close()
    if not pages:
        raise EngineError("E_NO_TEXT", "该 PDF 没有可提取的文字层（疑似扫描件）。请先用 OCR 生成可搜索 PDF。")

    client = Ollama(o["ollama_url"], o["model"], o["timeout"], o["temperature"], o["num_ctx"])
    try:
        client.version()
        if not client.has_model():
            raise EngineError("E_MODEL_MISSING", "Ollama 里没有模型 %s，请先执行：ollama pull %s" % (o["model"], o["model"]))
    except EngineError:
        raise
    except Exception as e:  # noqa
        raise EngineError("E_OLLAMA_DOWN", "无法连接 Ollama（%s）：%s" % (client.base, e))

    source_chunks = split_summary_text("\n\n".join(pages), 6000)
    partial_system = (
        "你是一名严谨的学术论文分析助手。请用简体中文概括给定论文片段，提取研究问题、方法/数据、"
        "主要结果、贡献与局限。忠实于原文；只总结片段明确提供的信息，不推测、不补充外部事实。"
        "如果片段是参考文献或页眉页脚，忽略它。控制在约 700 字以内。"
    )
    parts = []
    for index, chunk in enumerate(source_chunks):
        emit("progress", page=index + 1, total=len(source_chunks),
             message="正在分析论文内容（%d/%d）" % (index + 1, len(source_chunks)))
        parts.append(client.chat(
            partial_system,
            "论文标题：%s\n请分析以下论文片段：\n\n%s" % (title, chunk),
        ))

    for _ in range(5):
        previous_size = sum(len(part) for part in parts)
        if previous_size <= 4500:
            break
        groups = split_summary_text("\n\n".join(parts), 6000)
        reduced = []
        for index, group in enumerate(groups):
            emit("progress", page=index + 1, total=len(groups),
                 message="正在整合长论文摘要（%d/%d）" % (index + 1, len(groups)))
            reduced.append(client.chat(
                partial_system,
                "以下是同一篇论文不同部分的阶段性摘要。去重并保留核心信息：\n\n" + group,
            ))
        if not reduced or sum(len(part) for part in reduced) >= previous_size:
            raise EngineError("E_SUMMARY", "模型未能压缩长论文摘要，请更换模型或缩小论文范围后重试。")
        parts = reduced
    if sum(len(part) for part in parts) > 4500:
        raise EngineError("E_SUMMARY", "论文摘要过长，模型无法在上下文限制内完成整合。")

    final_system = (
        "你是一名严谨的学术论文阅读助手。根据提供的论文内容摘要，生成简体中文论文总结。"
        "只陈述有依据的信息；论文没有说明的内容请写“原文未说明”，不要臆测。"
        "使用以下 Markdown 标题组织内容：## 研究问题与目标、## 方法与数据、## 主要发现、"
        "## 创新与贡献、## 局限与后续方向、## 一句话总结。各节简明、具体，避免重复。"
    )
    result = client.chat(
        final_system,
        "论文标题：%s\n\n以下为论文各部分的阶段性摘要，请综合成一份连贯的论文总结：\n\n%s"
        % (title, "\n\n".join(parts)),
    )
    emit("summary", text=result)
    emit("done", summary={"chunks": len(source_chunks)})
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--summarize", action="store_true")
    args = ap.parse_args()
    try:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if args.check:
            return run_check(cfg)
        if args.summarize:
            return summarize_pdf(cfg)
        return translate_pdf(cfg)
    except EngineError as e:
        emit("error", code=e.code, message=e.message)
        return 2
    except Exception as e:  # noqa
        emit("error", code="E_UNKNOWN", message="%s: %s" % (type(e).__name__, e),
             trace=traceback.format_exc()[-1500:])
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

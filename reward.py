"""
奖励函数（Reward Function）—— 程序化评估生成 SVG 徽标的质量。

评分维度：
  1. 有效性      — SVG 是否为合法 XML、标签是否闭合、是否有 viewBox 等
  2. 结构合理性   — 坐标是否越界、颜色/元素数量是否合理
  3. 内容相关性   — 是否覆盖提示词中的关键词（颜色、形状、主题）
  4. 退化检测    — 是否空输出、重复内容、过于简单等

返回 0.0 ~ 1.0 的评分。
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

# ── 配置常量 ──────────────────────────────────────────────

VIEWBOX = "0 0 256 256"
VIEWBOX_MIN, VIEWBOX_MAX = 0, 256
# 允许坐标略微超出（SVG 描边/裁剪可能用到）
COORD_TOLERANCE = 30  # 允许坐标在 -30 ~ 286 之间

# 允许的 SVG 标签白名单
ALLOWED_TAGS = {
    "svg",
    "defs",
    "g",
    "path",
    "circle",
    "ellipse",
    "rect",
    "polygon",
    "polyline",
    "line",
    "text",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "use",
    "mask",
    "filter",
    "feGaussianBlur",
    "feOffset",
    "feMerge",
    "feMergeNode",
    "feColorMatrix",
    "title",
    "desc",
}

# 禁止的标签（会导致扣分严重）
FORBIDDEN_TAGS = {"image", "script", "iframe", "foreignObject", "style", "a", "animate"}

# 颜色相关的英文词（用于从提示词中提取颜色关键词）
COLOR_KEYWORDS = {
    # 基本色
    "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "brown", "black", "white", "gray", "grey", "cyan", "magenta",
    "teal", "navy", "gold", "silver", "bronze", "copper", "coral",
    "beige", "cream", "maroon", "olive", "lime", "mint", "peach",
    "lavender", "indigo", "violet", "turquoise", "aqua", "crimson",
    "amber", "ivory", "tan", "walnut", "mahogany", "plum", "rose",
    "salmon", "sand", "sapphire", "scarlet", "slate", "steel",
    "wheat", "wine", "rust", "ruby", "jade", "emerald", "ebony",
    "charcoal", "chocolate", "cherry", "chestnut", "bronze",
    "azure", "apricot", "mustard", "lemon", "khaki", "cobalt",
    # 修饰词
    "golden", "silvery", "metallic", "pastel", "neon", "dark",
    "light", "bright", "deep", "muted", "soft", "warm", "cool",
    "vibrant", "pale", "rich", "dull", "glossy", "matte",
    # 额外
    "navy blue", "teal green", "golden yellow", "warm golden",
    "deep navy", "soft blue", "dark gray", "light gray",
}

# 形状/元素关键词（从提示词中提取结构信息）
SHAPE_KEYWORDS = {
    "circle", "circular", "ring", "oval", "ellipse", "round",
    "square", "rectangle", "rectangular", "diamond", "triangle",
    "triangle", "hexagon", "hexagonal", "octagon", "star",
    "shield", "badge", "seal", "medallion", "coin", "emblem",
    "arrow", "leaf", "sprout", "plant", "stem", "bud", "seed",
    "wave", "curve", "curl", "swirl", "spiral", "arc",
    "line", "bar", "stripe", "dot", "tick", "cross", "checkmark",
    "note", "music", "column", "pillar", "house", "home", "roof",
    "rocket", "planet", "moon", "sun", "star", "globe",
    "heart", "crown", "flame", "fire", "water", "drop",
    "tree", "flower", "petal", "branch", "root",
    "gear", "cog", "wheel", "ribbon", "banner", "flag",
    "bird", "fish", "animal", "wing", "eye", "hand",
    "lock", "key", "shield", "sword", "anchor",
    "mountain", "hill", "cloud", "lightning", "bolt",
    "book", "pen", "pencil", "lamp", "bulb", "light",
    "nut", "almond", "bean", "coffee", "cup", "mug",
}

# 主题/风格关键词
STYLE_KEYWORDS = {
    "gradient", "shadow", "glow", "outline", "border", "frame",
    "minimal", "modern", "vintage", "classic", "geometric",
    "abstract", "clean", "crisp", "polished", "flat",
    "negative space", "silhouette", "icon", "symbol",
    "foundation", "trustworthy", "stable", "growth", "nurturing",
    "reliability", "protection", "security", "authority",
}


def _extract_svg_from_output(text: str) -> str:
    """从模型输出中提取纯 SVG 内容。

    处理常见情况：
    - 裸 SVG
    - markdown 代码块包裹
    - 前后有额外文字
    """
    t = text.strip()

    # 尝试提取 markdown 代码块中的 SVG
    m = re.search(r"```(?:svg|xml)?\s*(<svg[\s\S]*?</svg>)\s*```", t, re.IGNORECASE)
    if m:
        return m.group(1)

    # 提取第一个 <svg>...</svg>
    m = re.search(r"<svg[\s\S]*?</svg>", t, re.IGNORECASE)
    if m:
        return m.group(0)

    return t


def _parse_svg(svg_text: str) -> tuple[ET.Element | None, str]:
    """解析 SVG 文本，返回 (根元素, 错误信息)。

    成功时返回 (root, "")，失败时返回 (None, error_reason)。
    """
    try:
        root = ET.fromstring(svg_text)
        return root, ""
    except ET.ParseError as e:
        return None, str(e)


def _extract_coordinates_from_element(elem: ET.Element) -> list[float]:
    """从 SVG 元素中提取所有数值坐标。"""
    coords: list[float] = []
    numeric_attrs = {
        "cx", "cy", "r", "rx", "ry",
        "x", "y", "x1", "y1", "x2", "y2",
        "width", "height",
        "fx", "fy",
        "offset",
    }
    for attr_name in numeric_attrs:
        val = elem.get(attr_name)
        if val is not None:
            try:
                coords.append(float(val))
            except ValueError:
                pass

    # 解析 path 的 d 属性中的数字
    d = elem.get("d", "")
    if d:
        nums = re.findall(r"[-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", d)
        coords.extend(float(n) for n in nums)

    # 解析 points 属性（polygon/polyline）
    pts = elem.get("points", "")
    if pts:
        nums = re.findall(r"[-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", pts)
        coords.extend(float(n) for n in nums)

    # 解析 transform 中的 translate 值
    transform = elem.get("transform", "")
    if transform:
        nums = re.findall(r"[-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", transform)
        coords.extend(float(n) for n in nums)

    return coords


def _extract_colors(svg_text: str) -> list[str]:
    """从 SVG 文本中提取所有颜色引用。"""
    # hex 颜色 #RGB #RRGGBB
    hex_colors = re.findall(r"#[0-9A-Fa-f]{3,8}\b", svg_text)

    # rgb/rgba 颜色
    rgb_colors = re.findall(
        r"rgba?\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+[^)]*\)", svg_text, re.IGNORECASE
    )

    # 常见 SVG 命名颜色
    named_colors = re.findall(
        r"\b(none|currentColor|transparent|white|black|red|blue|green|yellow|"
        r"orange|purple|pink|brown|gray|grey|cyan|magenta|teal|navy|gold|"
        r"silver|lime|maroon|olive|aqua|coral|indigo|violet|turquoise|"
        r"crimson|ivory|tan|plum|salmon|wheat)\b",
        svg_text,
        re.IGNORECASE,
    )

    return hex_colors + rgb_colors + [c.lower() for c in named_colors]


def _extract_keywords_from_prompt(prompt: str, keyword_set: set[str]) -> set[str]:
    """从提示词中提取匹配的关键词。"""
    prompt_lower = prompt.lower()
    found: set[str] = set()
    for kw in keyword_set:
        if kw.lower() in prompt_lower:
            found.add(kw.lower())
    return found


# ── 1. 有效性验证 ──────────────────────────────────────────

def _score_validity(svg_text: str, root: ET.Element | None) -> tuple[float, dict[str, Any]]:
    """评估 SVG 的有效性。返回 (分数, 详情)。"""
    checks: dict[str, bool | float] = {}
    weight = 1.0

    # 1.1 非空
    checks["non_empty"] = len(svg_text.strip()) > 50

    # 1.2 有效的 XML
    checks["valid_xml"] = root is not None

    if root is None:
        # XML 解析失败 → 重大扣分
        return sum(1.0 for v in checks.values() if v) / max(len(checks), 1) * 0.3, checks

    # 1.3 根元素是 <svg>
    tag = root.tag
    # 去除命名空间前缀
    local_tag = tag.split("}")[-1] if "}" in tag else tag
    checks["root_is_svg"] = local_tag.lower() == "svg"

    # 1.4 有 xmlns 属性
    checks["has_xmlns"] = any(
        k.startswith("xmlns") for k in root.attrib
    )

    # 1.5 viewBox 正确
    vb = root.get("viewBox", "")
    checks["viewbox_correct"] = vb == VIEWBOX

    # 1.6 无禁用标签
    all_tags: set[str] = set()
    forbidden_found: set[str] = set()

    def _collect_tags(elem: ET.Element) -> None:
        t = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        all_tags.add(t.lower())
        if t.lower() in FORBIDDEN_TAGS:
            forbidden_found.add(t.lower())
        for child in elem:
            _collect_tags(child)

    if root is not None:
        _collect_tags(root)
    checks["no_forbidden_tags"] = len(forbidden_found) == 0

    # 1.7 所有标签在允许范围内
    unknown_tags = all_tags - ALLOWED_TAGS - FORBIDDEN_TAGS
    checks["all_tags_allowed"] = len(unknown_tags) == 0

    # 1.8 无 markdown 代码块残留
    raw = svg_text.strip()
    checks["no_markdown_fence"] = not raw.startswith("```")

    # 1.9 path 语法校验（每个 <path d="..."> 的 d 属性必须是合法 SVG 路径）
    checks["path_syntax_valid"] = _validate_all_paths(root)

    # 加权总分
    if not checks.get("root_is_svg", False):
        return 0.1, checks  # 根元素不对 → 几乎不可用

    passed = sum(1.0 for v in checks.values() if v)
    score = (passed / max(len(checks), 1)) * weight

    return score, checks


def _validate_all_paths(root: ET.Element) -> bool:
    """检查所有 <path> 的 d 属性是否语法合法（命令参数数量匹配）。"""
    CMD_ARGS = {
        "M": 2, "m": 2, "L": 2, "l": 2, "H": 1, "h": 1, "V": 1, "v": 1,
        "C": 6, "c": 6, "S": 4, "s": 4, "Q": 4, "q": 4, "T": 2, "t": 2,
        "A": 7, "a": 7, "Z": 0, "z": 0,
    }
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag.lower() != "path":
            continue
        d = elem.get("d", "")
        if not d:
            continue
        tokens = re.findall(r"[A-Za-z]|[-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", d)
        i = 0
        last_cmd = None
        while i < len(tokens):
            token = tokens[i]
            if token.upper() in CMD_ARGS:
                last_cmd = token.upper()
                expected = CMD_ARGS[last_cmd]
                if expected > 0:
                    # 检查后续 expected 个 token 是否都是数字
                    if i + expected >= len(tokens):
                        return False
                    for j in range(i + 1, i + 1 + expected):
                        if not re.match(r"^[-]?\d", tokens[j]):
                            return False  # 命令参数被另一个命令截断了
                i += 1 + expected
            elif re.match(r"^[-]?\d", token):
                # 隐式命令（重复上一个命令）—— 需要 last_cmd 的参数数量
                if last_cmd and last_cmd != "Z":
                    i += 1  # 跳过这个数字（简化处理）
                else:
                    i += 1
            else:
                return False  # 无法识别的 token
    return True


# ── 2. 结构合理性 ──────────────────────────────────────────

def _score_structure(svg_text: str, root: ET.Element) -> tuple[float, dict[str, Any]]:
    """评估 SVG 的结构合理性。"""
    checks: dict[str, bool | float] = {}

    # 2.1 坐标范围检查
    all_coords: list[float] = []

    def _collect_coords(elem: ET.Element) -> None:
        all_coords.extend(_extract_coordinates_from_element(elem))
        for child in elem:
            _collect_coords(child)

    _collect_coords(root)

    # 过滤非常大的背景矩形坐标（如 -9999）
    # 排除明显是背景填充的坐标
    relevant_coords = [
        c for c in all_coords
        if abs(c) < 10000  # 排除巨型背景
    ]

    if relevant_coords:
        out_of_bounds = sum(
            1 for c in relevant_coords
            if c < VIEWBOX_MIN - COORD_TOLERANCE or c > VIEWBOX_MAX + COORD_TOLERANCE
        )
        total = len(relevant_coords)
        # 允许少量越界，但大量越界扣分
        if total > 0:
            oob_ratio = out_of_bounds / total
            checks["coords_in_bounds"] = 1.0 - min(oob_ratio * 3, 1.0)  # 3x 惩罚因子
        else:
            checks["coords_in_bounds"] = 1.0
    else:
        checks["coords_in_bounds"] = 0.5  # 没有坐标 → 可疑

    # 2.2 颜色数量合理
    colors = _extract_colors(svg_text)
    distinct = len(set(c.lower() for c in colors))
    if distinct == 0:
        checks["color_count"] = 0.3
    elif distinct == 1:
        checks["color_count"] = 0.5  # 过少
    elif 2 <= distinct <= 8:
        checks["color_count"] = 1.0  # 理想范围
    elif 9 <= distinct <= 12:
        checks["color_count"] = 0.7  # 稍多但可接受
    else:
        checks["color_count"] = 0.3  # 过多颜色

    # 2.3 元素数量合理
    def _count_elements(elem: ET.Element) -> int:
        return 1 + sum(_count_elements(c) for c in elem)

    elem_count = _count_elements(root) - 1  # 排除 svg 根元素
    if elem_count <= 1:
        checks["element_count"] = 0.2  # 几乎没有内容
    elif elem_count <= 5:
        checks["element_count"] = 0.5  # 偏少
    elif 5 < elem_count <= 200:
        checks["element_count"] = 1.0  # 合理
    elif 200 < elem_count <= 500:
        checks["element_count"] = 0.6  # 偏多
    else:
        checks["element_count"] = 0.3  # 过多，可能是退化

    # 2.4 至少有一个有意义形状（不只是背景矩形）
    shapes = 0
    for elem in root.iter():
        t = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if t.lower() in {"path", "circle", "ellipse", "polygon", "polyline", "rect", "line"}:
            shapes += 1
    checks["has_shapes"] = min(shapes / 3, 1.0) if shapes > 0 else 0.0

    # 2.5 是否有 defs（好的 SVG 通常有渐变/滤镜）
    has_defs = any(
        (e.tag.split("}")[-1] if "}" in e.tag else e.tag).lower() == "defs"
        for e in root.iter()
    )
    # 不是必须的，有则加分
    checks["has_defs"] = 1.0 if has_defs else 0.8

    passed = sum(float(v) for v in checks.values())
    score = passed / max(len(checks), 1)
    return score, checks


# ── 3. 内容相关性 ──────────────────────────────────────────

def _score_relevance(prompt: str, svg_text: str) -> tuple[float, dict[str, Any]]:
    """评估 SVG 与提示词的内容相关性。"""
    checks: dict[str, bool | float] = {}

    svg_lower = svg_text.lower()

    # 3.1 颜色关键词覆盖
    prompt_colors = _extract_keywords_from_prompt(prompt, COLOR_KEYWORDS)
    if prompt_colors:
        matched_colors = sum(1 for c in prompt_colors if c in svg_lower)
        checks["color_match"] = matched_colors / len(prompt_colors)
    else:
        checks["color_match"] = 0.8  # 无颜色关键词，不扣不奖

    # 3.2 形状关键词覆盖
    prompt_shapes = _extract_keywords_from_prompt(prompt, SHAPE_KEYWORDS)
    if prompt_shapes:
        matched_shapes = sum(1 for s in prompt_shapes if s in svg_lower)
        checks["shape_match"] = matched_shapes / len(prompt_shapes)
    else:
        checks["shape_match"] = 0.8

    # 3.3 检查 SVG 中的 hex 颜色是否与提示词颜色匹配
    hex_colors_in_svg = set(re.findall(r"#[0-9A-Fa-f]{6}\b", svg_text))
    hex_colors_in_prompt = set(re.findall(r"#[0-9A-Fa-f]{6}\b", prompt))
    if hex_colors_in_prompt:
        overlap = hex_colors_in_svg & hex_colors_in_prompt
        checks["hex_color_match"] = len(overlap) / len(hex_colors_in_prompt)
    else:
        checks["hex_color_match"] = 0.8

    # 3.4 元素类型多样性（SVG 里有多种元素 → 更丰富）
    tag_types: set[str] = set()
    try:
        root, _ = _parse_svg(svg_text)
        if root is not None:
            for elem in root.iter():
                t = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                tag_types.add(t.lower())
    except Exception:
        pass

    shape_tags = tag_types & {"path", "circle", "ellipse", "rect", "polygon", "polyline", "line"}
    checks["element_diversity"] = min(len(shape_tags) / 4, 1.0)  # 理想情况 4+ 种

    # 3.5 长度合理性（太短说明没画东西）
    svg_len = len(svg_text)
    if svg_len < 200:
        checks["length_ok"] = 0.2
    elif svg_len < 500:
        checks["length_ok"] = 0.5
    elif svg_len < 5000:
        checks["length_ok"] = 1.0
    else:
        checks["length_ok"] = 0.7  # 过长也可能是退化

    passed = sum(float(v) for v in checks.values())
    score = passed / max(len(checks), 1)
    return score, checks


# ── 4. 退化检测 ────────────────────────────────────────────

def _detect_degeneration(svg_text: str, root: ET.Element | None) -> tuple[float, dict[str, Any]]:
    """检测退化/低质量输出。返回 (惩罚分, 检测结果)。

    惩罚分 >= 0，越大说明退化越严重。
    """
    penalties: dict[str, float] = {}

    svg_stripped = svg_text.strip()

    # 4.1 空或极短输出
    if len(svg_stripped) < 50:
        penalties["too_short"] = 0.8
    elif len(svg_stripped) < 150:
        penalties["very_short"] = 0.4

    # 4.2 没有 <svg> 标签
    if not re.search(r"<svg[\s>]", svg_stripped, re.IGNORECASE):
        penalties["no_svg_tag"] = 1.0

    # 4.3 包含 markdown 代码块
    if svg_stripped.startswith("```"):
        penalties["markdown_fence"] = 0.3

    # 4.4 重复内容检测（同一字符串连续重复多次）
    # 将 SVG 按行分组，检测是否有大量重复行
    lines = svg_stripped.split("\n")
    if len(lines) > 3:
        line_counts = Counter(lines)
        most_common_count = line_counts.most_common(1)[0][1]
        if most_common_count > len(lines) * 0.5 and most_common_count > 5:
            penalties["repeated_lines"] = 0.6
        elif most_common_count > len(lines) * 0.3 and most_common_count > 3:
            penalties["some_repetition"] = 0.3

    # 4.5 检查是否只有背景没有前景（没有任何有意义形状）
    if root is not None:
        shape_count = 0
        for elem in root.iter():
            t = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if t.lower() in {"path", "circle", "ellipse", "polygon", "polyline", "rect", "line"}:
                # 排除巨型背景 rect
                w = elem.get("width", "")
                h = elem.get("height", "")
                if w and h:
                    try:
                        if float(w) > 5000 or float(h) > 5000:
                            continue  # 跳过背景填充 rect
                    except ValueError:
                        pass
                shape_count += 1
        if shape_count == 0:
            penalties["no_shapes"] = 0.7
        elif shape_count == 1:
            penalties["single_shape"] = 0.3

    # 4.6 大量无效/占位元素
    # 检查是否有大量空 <g> 或 path d="" 等
    empty_paths = len(re.findall(r'<path[^>]*\sd\s*=\s*""', svg_stripped, re.IGNORECASE))
    if empty_paths > 3:
        penalties["empty_paths"] = 0.5

    # 4.7 检查是否包含明显的模型 refusal/错误文本
    bad_patterns = [
        r"(?i)(cannot|unable to|sorry|I apologize|I('|’)m sorry)",
        r"(?i)(as an AI|as a language model)",
        r"\bNaN\b",
        r"\bundefined\b",
        r"\bnull\b",  # 在 SVG 中一般不出现
    ]
    for pat in bad_patterns:
        if re.search(pat, svg_stripped):
            penalties["refusal_or_error"] = 0.8
            break

    # 上限为 1.0
    total_penalty = min(sum(penalties.values()), 1.0)
    return total_penalty, penalties


# ── 综合评分 ───────────────────────────────────────────────

def compute_reward(prompt: str, generated_text: str, verbose: bool = False) -> float:
    """计算单条生成结果的奖励分数。

    Args:
        prompt: 用户输入的详细视觉提示词
        generated_text: 模型原始输出（可能包含 markdown 包裹）
        verbose: 是否打印详细分解

    Returns:
        0.0 ~ 1.0 的评分
    """
    svg_text = _extract_svg_from_output(generated_text)
    root, parse_error = _parse_svg(svg_text)

    # 1. 有效性（权重 35%）
    v_score, v_checks = _score_validity(svg_text, root)

    # 2. 结构合理性（权重 25%）
    if root is not None:
        s_score, s_checks = _score_structure(svg_text, root)
    else:
        s_score, s_checks = 0.0, {"error": parse_error}

    # 3. 内容相关性（权重 20%）
    r_score, r_checks = _score_relevance(prompt, svg_text)

    # 4. 退化检测（权重 20%）—— 作为惩罚
    d_penalty, d_checks = _detect_degeneration(svg_text, root)

    # 综合分数
    total = (
        v_score * 0.35
        + s_score * 0.25
        + r_score * 0.20
        + (1.0 - d_penalty) * 0.20
    )

    # 额外惩罚：如果有效性太低，全部分数打折
    if v_score < 0.3:
        total *= 0.3  # 无效 SVG → 大幅降分

    if verbose:
        print(f"有效性:       {v_score:.3f}  {v_checks}")
        print(f"结构合理性:    {s_score:.3f}  {s_checks}")
        print(f"内容相关性:    {r_score:.3f}  {r_checks}")
        print(f"退化惩罚:      {d_penalty:.3f}  {d_checks}")
        print(f"最终得分:      {total:.3f}")

    return round(total, 4)


def batch_evaluate(
    samples: list[dict[str, str]],
    verbose: bool = False,
) -> dict[str, Any]:
    """批量评估，返回统计信息。

    Args:
        samples: [{"prompt": ..., "generated": ...}, ...]
        verbose: 是否逐条打印

    Returns:
        {
            "scores": [score1, score2, ...],
            "mean": ...,
            "median": ...,
            "min": ...,
            "max": ...,
            "std": ...,
            "valid_rate": ...  # SVG 有效性比例
        }
    """
    scores: list[float] = []
    valid_count = 0

    for i, sample in enumerate(samples):
        prompt = sample["prompt"]
        generated = sample["generated"]
        score = compute_reward(prompt, generated, verbose=False)

        svg_text = _extract_svg_from_output(generated)
        root, _ = _parse_svg(svg_text)
        if root is not None:
            valid_count += 1

        scores.append(score)

        if verbose:
            print(f"[{i}] score={score:.3f}  prompt={prompt[:60]}...")

    # 统计
    n = len(scores)
    mean = sum(scores) / n if n > 0 else 0.0
    sorted_scores = sorted(scores)
    median = sorted_scores[n // 2] if n > 0 else 0.0
    variance = sum((s - mean) ** 2 for s in scores) / n if n > 0 else 0.0

    return {
        "scores": scores,
        "mean": round(mean, 4),
        "median": round(median, 4),
        "min": round(min(scores), 4) if scores else 0.0,
        "max": round(max(scores), 4) if scores else 0.0,
        "std": round(math.sqrt(variance), 4),
        "valid_rate": round(valid_count / n, 4) if n > 0 else 0.0,
        "count": n,
    }


# ── 测试入口 ───────────────────────────────────────────────

if __name__ == "__main__":
    # 加载 valid.jsonl 测试
    data_path = "valid.jsonl"
    samples: list[dict[str, str]] = []
    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            msgs = item["messages"]
            user_msg = next(m["content"] for m in msgs if m["role"] == "user")
            asst_msg = next(m["content"] for m in msgs if m["role"] == "assistant")
            samples.append({"prompt": user_msg, "generated": asst_msg})

    # 这些是 Sonnet 生成的 ground-truth，应该都得高分
    result = batch_evaluate(samples, verbose=True)
    print(f"\n{'='*50}")
    print(f"Ground-truth (Sonnet) 评估结果:")
    for k, v in result.items():
        if k != "scores":
            print(f"  {k}: {v}")

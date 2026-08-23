"""
국내주식 분석 리포트 PDF 렌더러 (ReportLab).

`kr_report_service` 가 만든 리포트 컨텍스트(dict)를 받아 A4 PDF 로 그린다.
LLM 이 쓴 서술(narrative)이 없어도 기계적 데이터만으로 완전한 리포트가 나오도록
모든 섹션이 독립적으로 동작한다 — LLM 실패가 리포트 자체를 없애면 안 되기 때문이다.

한글 폰트:
  1순위 TTF (Windows 맑은고딕 / Linux 나눔고딕·Noto CJK) — 자간·굵기가 자연스럽다
  2순위 ReportLab 내장 CID 폰트(HYSMyeongJo-Medium) — 폰트 파일이 없는 서버용 폴백
"""
import logging
import os
from datetime import datetime
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# ── 색상 팔레트 ────────────────────────────────────────────────
NAVY = colors.HexColor("#1e293b")
BLUE = colors.HexColor("#2563eb")
GREY = colors.HexColor("#64748b")
LIGHT = colors.HexColor("#f1f5f9")
BORDER = colors.HexColor("#cbd5e1")
RED = colors.HexColor("#dc2626")

# 후보 TTF 경로 (앞에서부터 존재하는 것을 사용)
_TTF_CANDIDATES = [
    (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
     "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", None),
]

_FONT = None       # 본문 폰트명
_FONT_BOLD = None  # 굵은 폰트명


def _register_fonts():
    """한글 폰트 등록 (프로세스당 1회)."""
    global _FONT, _FONT_BOLD
    if _FONT:
        return

    for regular, bold in _TTF_CANDIDATES:
        if not os.path.exists(regular):
            continue
        try:
            pdfmetrics.registerFont(TTFont("KRBody", regular))
            _FONT = "KRBody"
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("KRBodyBold", bold))
                _FONT_BOLD = "KRBodyBold"
            else:
                _FONT_BOLD = "KRBody"
            logger.info(f"리포트 폰트: {regular}")
            return
        except Exception as e:
            logger.warning(f"폰트 등록 실패({regular}): {e}")

    # 폰트 파일이 없는 서버 — ReportLab 내장 CJK CID 폰트로 폴백
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    _FONT = _FONT_BOLD = "HYSMyeongJo-Medium"
    logger.info("리포트 폰트: 내장 CID(HYSMyeongJo-Medium) 폴백")


def _styles() -> dict:
    _register_fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "KRTitle", parent=base["Title"], fontName=_FONT_BOLD, fontSize=20,
            leading=26, textColor=NAVY, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "KRSub", fontName=_FONT, fontSize=9.5, leading=14,
            textColor=GREY, alignment=TA_CENTER, spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "KRH1", fontName=_FONT_BOLD, fontSize=13, leading=18,
            textColor=BLUE, spaceBefore=14, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "KRH2", fontName=_FONT_BOLD, fontSize=10.5, leading=15,
            textColor=NAVY, spaceBefore=8, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "KRBodyP", fontName=_FONT, fontSize=9.5, leading=15, spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "KRSmall", fontName=_FONT, fontSize=8, leading=11.5, textColor=GREY,
        ),
        "cell": ParagraphStyle("KRCell", fontName=_FONT, fontSize=8, leading=11),
        "cellb": ParagraphStyle("KRCellB", fontName=_FONT_BOLD, fontSize=8, leading=11),
        "headline": ParagraphStyle(
            "KRHeadline", fontName=_FONT_BOLD, fontSize=11.5, leading=17,
            textColor=NAVY, spaceAfter=4,
        ),
    }


# ── 포맷 헬퍼 ──────────────────────────────────────────────────

def _won(v: Optional[float], suffix: str = "원") -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.0f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _num(v: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _pct(v: Optional[float], digits: int = 2, signed: bool = False) -> str:
    if v is None:
        return "-"
    try:
        fmt = f"{{:+,.{digits}f}}%" if signed else f"{{:,.{digits}f}}%"
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return "-"


def _esc(text) -> str:
    """Paragraph 는 마크업을 해석하므로 & < > 를 이스케이프한다."""
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paras(text: str, style) -> List[Paragraph]:
    """줄바꿈 기준으로 문단 분리 (LLM 서술은 여러 문단으로 온다)."""
    if not text:
        return []
    return [
        Paragraph(_esc(chunk.strip()), style)
        for chunk in str(text).split("\n")
        if chunk.strip()
    ]


def _table(data, widths, styles, align_right=(), header=True) -> Table:
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ]
    cmds.append(("FONTNAME", (0, 1 if header else 0), (-1, -1), _FONT))
    cmds.append(("FONTSIZE", (0, 1 if header else 0), (-1, -1), 8))
    for col in align_right:
        cmds.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return t


def _section(title: str, styles) -> List:
    return [
        Paragraph(_esc(title), styles["h1"]),
        HRFlowable(width="100%", thickness=0.8, color=BLUE, spaceAfter=6),
    ]


# ── 섹션 빌더 ──────────────────────────────────────────────────

def _summary_block(ctx: dict, styles) -> List:
    """표지 하단 핵심 요약 카드."""
    quote = ctx.get("quote") or {}
    market = ctx.get("market") or {}
    approved = ctx.get("approved") or []
    held = ctx.get("held") or []
    sell = [d for d in (ctx.get("sell_decisions") or []) if d.get("decision") != "HOLD"]

    rows = [
        ["매수 예약", f"{len(approved)}종목", "보류(HOLD)", f"{len(held)}종목"],
        [
            "예상 투입금액",
            _won(quote.get("total_amount")),
            "총자산 대비",
            _pct(quote.get("total_ratio_pct")),
        ],
        [
            "코스피",
            _num(market.get("kospi"), 2),
            "20일 변동성",
            _pct(market.get("kospi_vol_20d"), 1),
        ],
        [
            "원/달러",
            _num(market.get("usdkrw"), 1, "원"),
            "매도 판정",
            f"{len(sell)}건",
        ],
    ]
    t = Table(rows, colWidths=[32 * mm, 45 * mm, 32 * mm, 45 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTNAME", (0, 0), (0, -1), _FONT_BOLD),
        ("FONTNAME", (2, 0), (2, -1), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("BACKGROUND", (2, 0), (2, -1), LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [t]


def _market_section(ctx: dict, styles) -> List:
    market = ctx.get("market") or {}
    narrative = ctx.get("narrative") or {}
    story = _section("1. 시장 진단", styles)

    rows = [["지표", "값", "지표", "값"]]
    pairs = [
        ("코스피", _num(market.get("kospi"), 2)),
        ("코스피200", _num(market.get("kospi200"), 2)),
        ("코스피 20일 실현변동성", _pct(market.get("kospi_vol_20d"), 1)),
        ("VIX", _num(market.get("vix"), 2)),
        ("원/달러", _num(market.get("usdkrw"), 1, "원")),
        ("원/달러 20일 변화", _pct(market.get("usdkrw_chg_20d"), 2, signed=True)),
        ("기준일", _esc(market.get("date") or "-")),
        ("변동성 게이트", _esc((market.get("override") or {}).get("summary")
                          if isinstance(market.get("override"), dict)
                          else market.get("override") or "정상")),
    ]
    for i in range(0, len(pairs), 2):
        left = pairs[i]
        right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        rows.append([left[0], left[1], right[0], right[1]])

    story.append(_table(rows, [42 * mm, 34 * mm, 42 * mm, 34 * mm], styles,
                        align_right=(1, 3)))
    story.append(Spacer(1, 6))

    if narrative.get("market_view"):
        story += _paras(narrative["market_view"], styles["body"])
    if ctx.get("llm_reasoning"):
        story.append(Paragraph("LLM 매수검토 시장 코멘트", styles["h2"]))
        story += _paras(ctx["llm_reasoning"], styles["body"])
    return story


def _quote_section(ctx: dict, styles) -> List:
    """매수 견적서 — 다음 영업일 집행 예정 주문."""
    quote = ctx.get("quote") or {}
    rows_data = quote.get("rows") or []
    story = _section(
        f"2. 매수 견적서 (집행 예정 {ctx.get('execution_time', '09:05')} KST)", styles
    )

    if not rows_data:
        story.append(Paragraph(
            _esc(quote.get("note") or "오늘 예약된 매수 종목이 없습니다."), styles["body"]
        ))
        return story

    header = ["#", "종목", "코드", "섹터", "종합점수", "예상단가", "수량", "예상금액", "비중"]
    data = [header]
    for i, r in enumerate(rows_data, 1):
        data.append([
            str(i),
            Paragraph(_esc(r.get("stock_name")), styles["cellb"]),
            _esc(r.get("code")),
            Paragraph(_esc(r.get("sector") or "-"), styles["cell"]),
            _num(r.get("composite_score"), 2),
            _won(r.get("ref_price")),
            f"{r.get('quantity')}주" if r.get("quantity") is not None else "-",
            _won(r.get("amount")),
            _pct(r.get("ratio_pct"), 1),
        ])
    data.append([
        "", Paragraph("합계", styles["cellb"]), "", "", "", "",
        f"{quote.get('total_quantity', 0)}주",
        _won(quote.get("total_amount")),
        _pct(quote.get("total_ratio_pct"), 1),
    ])

    t = _table(
        data,
        [8 * mm, 30 * mm, 14 * mm, 24 * mm, 17 * mm, 22 * mm, 14 * mm, 25 * mm, 14 * mm],
        styles,
        align_right=(4, 5, 6, 7, 8),
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#e2e8f0")),
        ("FONTNAME", (0, len(data) - 1), (-1, len(data) - 1), _FONT_BOLD),
    ]))
    story.append(t)
    story.append(Spacer(1, 5))

    basis = []
    if quote.get("total_assets"):
        basis.append(f"총자산 {_won(quote['total_assets'])}")
    if quote.get("cash") is not None:
        basis.append(f"D+2 예수금 {_won(quote['cash'])}")
    basis.append(
        f"배분 {quote.get('method', '-')} / tilt {quote.get('tilt', '-')} "
        f"(기준 {_pct(quote.get('base_ratio_pct'), 0)}, 종목당 "
        f"{_pct(quote.get('min_ratio_pct'), 0)}~{_pct(quote.get('max_ratio_pct'), 0)})"
    )
    story.append(Paragraph("산출 기준: " + " · ".join(basis), styles["small"]))
    story.append(Paragraph(
        _esc(quote.get("note") or
             "예상단가는 분석 시점 종가 기준입니다. 실제 주문은 집행 시각의 현재가를 "
             "재조회해 호가단위로 올림한 지정가로 나가며, 수량도 그때 다시 계산됩니다."),
        styles["small"],
    ))
    return story


def _thesis_section(ctx: dict, styles) -> List:
    narrative = ctx.get("narrative") or {}
    theses = narrative.get("buy_thesis") or []
    approved = {c.get("code"): c for c in (ctx.get("approved") or [])}
    if not theses and not approved:
        return []

    story = _section("3. 종목별 매수 논거", styles)

    if theses:
        for t in theses:
            code = t.get("code")
            cand = approved.get(code, {})
            head = f"{t.get('stock_name') or cand.get('stock_name') or ''} ({code})"
            conf = t.get("confidence")
            if conf:
                head += f"  ·  확신도 {conf}"
            block = [Paragraph(_esc(head), styles["h2"])]
            if cand:
                block.append(Paragraph(
                    _esc(
                        f"ML 예측 상승률 {_pct(cand.get('rise_probability'))} "
                        f"(정확도 {_pct(cand.get('accuracy'), 1)}) · "
                        f"RSI {_num(cand.get('rsi'), 1)} · "
                        f"ADX {_num(cand.get('adx'), 1)} · "
                        f"감성 {_num(cand.get('sentiment_score'), 2)} · "
                        f"종합점수 {_num(cand.get('composite_score'), 2)}"
                    ),
                    styles["small"],
                ))
            block += _paras(t.get("thesis"), styles["body"])
            if t.get("risk"):
                block.append(Paragraph(
                    f"<b>리스크</b> — {_esc(t['risk'])}",
                    ParagraphStyle("risk", parent=styles["body"], textColor=RED),
                ))
            story.append(KeepTogether(block))
            story.append(Spacer(1, 3))
    else:
        # LLM 서술이 없을 때도 승인 종목의 판정 사유는 남긴다
        for c in approved.values():
            story.append(Paragraph(
                _esc(f"{c.get('stock_name')} ({c.get('code')})"), styles["h2"]
            ))
            story += _paras(c.get("llm_reason"), styles["body"])
    return story


def _hold_section(ctx: dict, styles) -> List:
    held = ctx.get("held") or []
    if not held:
        return []
    story = _section("4. 보류(HOLD) 종목", styles)
    data = [["종목", "코드", "종합점수", "상승률", "보류 사유"]]
    for c in held:
        data.append([
            Paragraph(_esc(c.get("stock_name")), styles["cell"]),
            _esc(c.get("code")),
            _num(c.get("composite_score"), 2),
            _pct(c.get("rise_probability")),
            Paragraph(_esc(c.get("llm_reason")), styles["cell"]),
        ])
    story.append(_table(
        data, [28 * mm, 14 * mm, 17 * mm, 17 * mm, 92 * mm], styles, align_right=(2, 3)
    ))
    return story


def _holdings_section(ctx: dict, styles) -> List:
    decisions = ctx.get("sell_decisions") or []
    if not decisions and not ctx.get("sell_market_analysis"):
        return []

    story = _section("5. 보유 포지션 매도검토", styles)
    if ctx.get("sell_market_analysis"):
        story += _paras(ctx["sell_market_analysis"], styles["body"])

    if decisions:
        data = [["종목", "코드", "판정", "사유"]]
        for d in decisions:
            data.append([
                Paragraph(_esc(d.get("stock_name")), styles["cell"]),
                _esc(d.get("code")),
                Paragraph(_esc(d.get("decision")),
                          styles["cellb"] if d.get("decision") != "HOLD" else styles["cell"]),
                Paragraph(_esc(d.get("reason")), styles["cell"]),
            ])
        t = _table(data, [28 * mm, 14 * mm, 24 * mm, 102 * mm], styles)
        for i, d in enumerate(decisions, 1):
            if d.get("decision") != "HOLD":
                t.setStyle(TableStyle([("TEXTCOLOR", (2, i), (2, i), RED)]))
        story.append(t)

    narrative = ctx.get("narrative") or {}
    if narrative.get("portfolio_action"):
        story.append(Paragraph("포트폴리오 조치", styles["h2"]))
        story += _paras(narrative["portfolio_action"], styles["body"])
    return story


def _risk_section(ctx: dict, styles) -> List:
    narrative = ctx.get("narrative") or {}
    risks = narrative.get("risk_factors") or []
    checklist = narrative.get("tomorrow_checklist") or []
    if not risks and not checklist:
        return []

    story = _section("6. 리스크 및 체크리스트", styles)
    if risks:
        story.append(Paragraph("주요 리스크", styles["h2"]))
        for r in risks:
            story.append(Paragraph(f"• {_esc(r)}", styles["body"]))
    if checklist:
        story.append(Paragraph("다음 영업일 확인 항목", styles["h2"]))
        for c in checklist:
            story.append(Paragraph(f"□ {_esc(c)}", styles["body"]))
    return story


def _appendix_section(ctx: dict, styles) -> List:
    """부록 — 채점 통과 후보 전체(LLM 검토 이전 원자료)."""
    candidates = ctx.get("all_candidates") or []
    if not candidates:
        return []

    story = [PageBreak()] + _section("부록. 채점 통과 후보 전체", styles)
    story.append(Paragraph(
        "LLM 검토 이전, 임계값을 통과한 후보 전체입니다. 종합점수 내림차순.", styles["small"]
    ))
    story.append(Spacer(1, 4))

    data = [["종목", "코드", "점수", "상승률", "정확도", "RSI", "ADX", "감성", "5일수급", "판정"]]
    for c in candidates:
        data.append([
            Paragraph(_esc(c.get("stock_name")), styles["cell"]),
            _esc(c.get("code")),
            _num(c.get("composite_score"), 2),
            _pct(c.get("rise_probability"), 1),
            _pct(c.get("accuracy"), 1),
            _num(c.get("rsi"), 1),
            _num(c.get("adx"), 1),
            _num(c.get("sentiment_score"), 2),
            _num(c.get("net_buy_score"), 0),
            _esc(c.get("llm_decision") or "-"),
        ])
    story.append(_table(
        data,
        [28 * mm, 14 * mm, 13 * mm, 15 * mm, 15 * mm, 12 * mm, 12 * mm, 13 * mm,
         19 * mm, 17 * mm],
        styles,
        align_right=(2, 3, 4, 5, 6, 7, 8),
    ))
    return story


def _footer(canvas, doc):
    """페이지 하단 — 면책 문구 + 페이지 번호."""
    canvas.saveState()
    canvas.setFont(_FONT or "Helvetica", 7)
    canvas.setFillColor(GREY)
    canvas.drawString(
        18 * mm, 12 * mm,
        "자동 생성 리포트 — 투자 판단의 참고 자료이며 투자 권유가 아닙니다. 최종 책임은 투자자 본인에게 있습니다.",
    )
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"- {doc.page} -")
    canvas.restoreState()


def build_report_pdf(ctx: dict, out_path: str) -> str:
    """리포트 컨텍스트 → PDF 파일 생성. 저장된 경로를 반환한다."""
    styles = _styles()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    generated_at = ctx.get("generated_at") or datetime.now()
    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
        title=f"국내주식 분석 리포트 {ctx.get('date', '')}",
        author="stock-ai-agent",
        subject="KOSPI 자동매매 일일 분석 리포트",
    )

    story: List = [
        Paragraph("국내주식 분석 리포트", styles["title"]),
        Paragraph(
            _esc(
                f"{ctx.get('date', '')} · {ctx.get('mode', '')} · "
                f"생성 {generated_at:%Y-%m-%d %H:%M} KST"
            ),
            styles["subtitle"],
        ),
        HRFlowable(width="100%", thickness=1.2, color=NAVY, spaceAfter=10),
    ]

    narrative = ctx.get("narrative") or {}
    if narrative.get("headline"):
        story.append(Paragraph(_esc(narrative["headline"]), styles["headline"]))
        story.append(Spacer(1, 4))
    if ctx.get("narrative_error"):
        story.append(Paragraph(
            f"※ LLM 리포트 서술 생성 실패 — 기계적 데이터만 수록했습니다 "
            f"({_esc(ctx['narrative_error'])[:200]})",
            ParagraphStyle("warn", parent=styles["small"], textColor=RED),
        ))
        story.append(Spacer(1, 4))

    story += _summary_block(ctx, styles)
    story += _market_section(ctx, styles)
    story += _quote_section(ctx, styles)
    story += _thesis_section(ctx, styles)
    story += _hold_section(ctx, styles)
    story += _holdings_section(ctx, styles)
    story += _risk_section(ctx, styles)
    story += _appendix_section(ctx, styles)

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    logger.info(f"리포트 PDF 생성: {out_path} ({os.path.getsize(out_path):,} bytes)")
    return out_path

# -*- coding: utf-8 -*-
"""
제안요청서 법령·지침 최신화 체크 도구 (웹 버전)
=================================================
국가법령정보센터(law.go.kr) Open API를 실시간으로 호출해서
입력한 법령/지침명이 현재 시행 중인 정식 명칭·최신 시행일자와
일치하는지 웹에서 바로 확인할 수 있는 도구입니다.

로컬 실행: streamlit run app.py
배포: Streamlit Community Cloud에 이 저장소를 연결하면 자동 배포됩니다.
"""
import io
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

import pandas as pd
import streamlit as st
# ------------------------------------------------------------------
# 기본 설정
# ------------------------------------------------------------------
st.set_page_config(
    page_title="제안요청서 법령·지침 최신화 체크 도구",
    page_icon="📋",
    layout="wide",
)

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"

TARGET_MAP = {
    "법령": "law",
    "행정규칙": "admrul",
    "자치법규": "ordin",
}
GUBUN_LABELS = {"law": "법령", "admrul": "행정규칙", "ordin": "자치법규"}


# ------------------------------------------------------------------
# 국가법령정보센터 API 호출 & 파싱 (기존 check_laws.py 로직 재사용)
# ------------------------------------------------------------------
def http_get(url, params, retries=2, timeout=8):
    query = urllib.parse.urlencode(params, encoding="utf-8")
    full_url = f"{url}?{query}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(0.8 * attempt)
    raise RuntimeError(f"API 호출 실패: {last_err}") from last_err


def find_first_text(elem, candidate_tags):
    for tag in candidate_tags:
        found = elem.find(f".//{tag}")
        if found is not None and found.text:
            return found.text.strip()
    return None


def parse_search_result(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return {"error": f"XML 파싱 실패: {e}"}

    total_count = find_first_text(root, ["totalCnt"])
    if total_count is not None and total_count.strip() == "0":
        return {"error": "검색결과 없음"}

    items = []
    for tag in ["law", "admrul", "ordin"]:
        found = root.findall(f".//{tag}")
        if found:
            items = found
            break
    if not items:
        children = list(root)
        if children:
            for child in children[1:]:
                if list(child):
                    items = [child]
                    break
    if not items:
        return {"error": "응답 구조를 해석하지 못함"}

    candidates = []
    for item in items:
        nm = find_first_text(item, ["법령명한글", "행정규칙명", "자치법규명", "법령명"])
        if not nm:
            continue
        candidates.append({
            "name": nm,
            "proclaim_date": find_first_text(item, ["공포일자"]),
            "enforce_date": find_first_text(item, ["시행일자"]),
            "dept": find_first_text(item, ["소관부처명", "소관부처"]),
        })

    if not candidates:
        return {"error": "응답에서 법령명을 찾지 못함"}

    first = candidates[0]
    return {
        "name": first["name"],
        "proclaim_date": first["proclaim_date"],
        "enforce_date": first["enforce_date"],
        "dept": first["dept"],
        "candidates": candidates,
        "error": None,
    }


def format_date(raw):
    if not raw:
        return ""
    raw = raw.strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}.{raw[4:6]}.{raw[6:]}"
    return raw


def normalize_name(s):
    if not s:
        return ""
    return re.sub(r"\s+", "", s)


# ------------------------------------------------------------------
# 제안요청서 본문에서 법령명 자동 추출
# ------------------------------------------------------------------
LAW_SUFFIXES = (
    "법", "법률", "시행령", "시행규칙", "기본법",
    "지침", "고시", "예규", "훈령", "조례", "규칙", "요령", "기준",
)

NOISE_PATTERNS = [
    re.compile(r'^제\s*\d+\s*장'),
    re.compile(r'다음\s*각\s*호'),
    re.compile(r'수행자'),
    re.compile(r'^(관련|기타|해당|위|아래)\s'),
    re.compile(r'(관련|각\s*호의)\s*법'),
    re.compile(r'^(및|또|그|이하|령)'),
    re.compile(r'하여야|해야|한다$|따른다$'),
    re.compile(r'^\s*$'),
    # 앞 문장을 가리키는 지시어 — 그 자체로는 법령명이 아니다.
    # 예: "동법 시행령", "같은 법 시행규칙", "본 법", "위 법률"
    re.compile(r'^(동법|동\s|같은\s*법|본\s*법|당해|상기|전기)'),
    re.compile(r'^(위|아래|앞)\s*(법|법률)'),
]

SHORT_LAW_WHITELIST = {
    "민법", "상법", "헌법", "형법", "세법", "특허법", "저작권법",
}


def clean_law_name(name):
    """추출된 문자열에서 조항·부가설명·번호·조사를 떼어내 법령명만 남긴다."""
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r'^\s*\d+[.)]\s*', '', s)
    s = re.sub(r'^[ㅇ□○·▪▫◦\-\s]+', '', s)
    s = re.sub(r'\s*제\s*\d+\s*조.*$', '', s)
    s = re.sub(r'\s*제\s*\d+\s*항.*$', '', s)
    s = re.sub(r'\s*\([^)]*\)\s*$', '', s)
    s = re.sub(r'(및|과|와|등|을|를|은|는|이|가)$', '', s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def looks_like_law(name):
    """법령명처럼 보이는지 판정해 문장 조각 등 노이즈를 걸러낸다."""
    if not name or len(name) < 2 or len(name) > 45:
        return False
    if not name.endswith(LAW_SUFFIXES):
        return False
    for pat in NOISE_PATTERNS:
        if pat.search(name):
            return False
    if name.endswith("법") and len(name) <= 3 and name not in SHORT_LAW_WHITELIST:
        return False
    return True


def extract_law_names(text):
    """
    본문에서 법령명을 추출한다.
    공문서는 법령명을 「 」 안에 넣는 관행이 있어 이를 1차 기준으로 삼고,
    낫표가 거의 없는 문서는 줄 단위 목록 형태만 보조로 인정한다.
    """
    results = []
    bracket_matches = re.findall(r'[「『]([^」』]{2,60})[」』]', text)
    for m in bracket_matches:
        c = clean_law_name(m)
        if looks_like_law(c) and c not in results:
            results.append(c)

    # 낫표 없이 적힌 지침·기준·고시도 함께 잡는다.
    # (실제 제안요청서는 「법령」과 낫표 없는 지침이 섞여 있는 경우가 많다)
    # 문장 중간이 아니라, 한 줄이 곧 하나의 항목인 목록 형태만 인정한다.
    for line in text.split('\n'):
        line = line.strip()
        if not line or len(line) > 50:
            continue
        # 이미 낫표로 잡은 줄은 건너뛴다 (중복·부분추출 방지)
        if '「' in line or '『' in line:
            continue
        c = clean_law_name(line)
        if looks_like_law(c) and c not in results:
            results.append(c)

    return results


def read_document_text(uploaded_file):
    """
    업로드된 파일에서 텍스트를 추출한다.
    hwp / hwpx / docx / txt 를 지원하며, 실패 시 (None, 오류메시지)를 반환한다.
    """
    name = uploaded_file.name.lower()
    raw_bytes = uploaded_file.read()

    if name.endswith(".txt"):
        for enc in ("utf-8", "cp949", "utf-8-sig"):
            try:
                return raw_bytes.decode(enc), None
            except UnicodeDecodeError:
                continue
        return None, "텍스트 파일 인코딩을 인식하지 못했습니다."

    if name.endswith(".docx"):
        try:
            import docx  # python-docx
            doc = docx.Document(io.BytesIO(raw_bytes))
            parts = [p.text for p in doc.paragraphs]
            # 표 안의 텍스트도 수집 (법령 목록이 표에 들어있는 경우가 많음)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        parts.append(cell.text)
            return "\n".join(parts), None
        except Exception as e:
            return None, f"docx 파일을 읽지 못했습니다: {e}"

    if name.endswith(".hwpx") or name.endswith(".hwp"):
        from hwp_reader import extract_text_auto
        text, err = extract_text_auto(raw_bytes, uploaded_file.name)
        if err:
            return None, (
                f"{err}\n\n"
                "문서보안(DRM)이 적용된 파일이거나 형식이 특이한 경우일 수 있습니다. "
                "한글에서 '다른 이름으로 저장'으로 hwpx 또는 docx로 저장해 다시 올려보시거나, "
                "아래 '본문을 붙여넣어 검사하기'를 이용해주세요."
            )
        return text, None

    return None, "지원하지 않는 파일 형식입니다. (hwp, hwpx, docx, txt만 가능)"


def _search_one_target(oc, target, query_name):
    params = {"OC": oc, "target": target, "type": "XML", "query": query_name, "display": 20}
    try:
        raw = http_get(SEARCH_URL, params)
    except Exception as e:
        return {"error": f"네트워크 오류: {e}"}

    result = parse_search_result(raw)
    if result.get("error"):
        return result

    # 후보 중 입력명과 정확히 일치하는 것이 있으면 그것을 대표로 올린다.
    # (법령센터는 관련도순으로 주기 때문에 1번이 정답이 아닐 수 있다)
    target_norm = normalize_name(query_name)
    for cand in result.get("candidates", []):
        if normalize_name(cand["name"]) == target_norm:
            result["name"] = cand["name"]
            result["proclaim_date"] = cand["proclaim_date"]
            result["enforce_date"] = cand["enforce_date"]
            result["dept"] = cand["dept"]
            break

    result["searched_target"] = target
    return result


def _similarity(a, b):
    """두 법령명의 유사도(0~1). 표준 difflib 사용."""
    import difflib
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def check_one(oc, gubun, query_name):
    """
    지정 구분으로 우선 검색하고, 정확히 일치하지 않으면 다른 구분으로도 재시도한다.
    어느 구분에서도 정확 일치가 없으면, 모든 구분에서 모은 후보를 유사도 순으로
    정렬해 'similar_candidates'로 돌려준다 (담당자가 바로 판단할 수 있도록).
    """
    primary_target = TARGET_MAP.get(gubun, "law")
    order = [primary_target] + [t for t in ("law", "admrul", "ordin") if t != primary_target]

    all_candidates = []   # (유사도, 후보dict, target)
    first_result = None

    for idx, target in enumerate(order):
        result = _search_one_target(oc, target, query_name)
        if idx == 0:
            first_result = result

        if result.get("error"):
            continue

        # 정확 일치를 찾으면 즉시 확정
        if result.get("name") and normalize_name(result["name"]) == normalize_name(query_name):
            result["matched_target"] = target
            if target != primary_target:
                result["auto_corrected_from"] = GUBUN_LABELS.get(primary_target, primary_target)
                result["auto_corrected_to"] = GUBUN_LABELS.get(target, target)
            return result

        # 정확 일치가 아니면 후보로 쌓아둔다
        for cand in result.get("candidates", []):
            all_candidates.append((
                _similarity(query_name, cand["name"]),
                cand,
                target,
            ))

    # 정확 일치 없음 -> 유사 후보를 유사도 순으로 정리해 반환
    all_candidates.sort(key=lambda x: x[0], reverse=True)

    seen = set()
    similar = []
    for score, cand, target in all_candidates:
        key = normalize_name(cand["name"])
        if key in seen:
            continue
        seen.add(key)
        similar.append({
            "name": cand["name"],
            "enforce_date": cand["enforce_date"],
            "dept": cand["dept"],
            "gubun": GUBUN_LABELS.get(target, target),
            "similarity": round(score * 100),
        })
        if len(similar) >= 5:
            break

    base = first_result if first_result else {"error": "검색결과 없음"}
    if similar:
        base = dict(base)
        base["error"] = None if base.get("name") else base.get("error")
        base["similar_candidates"] = similar
    base["matched_target"] = primary_target
    return base


# ------------------------------------------------------------------
# 화면 구성
# ------------------------------------------------------------------
st.title("📋 제안요청서 법령·지침 최신화 체크 도구")
st.caption("국가법령정보센터(law.go.kr) Open API를 실시간으로 조회하여 "
           "법령명·시행일자·소관부처를 자동으로 확인합니다.")

with st.sidebar:
    st.header("⚙️ 설정")
    try:
        default_oc = st.secrets.get("LAW_API_OC", "")
    except Exception:
        default_oc = ""
    oc = st.text_input(
        "API 인증키 (OC)",
        value=default_oc,
        help="law.go.kr OPEN API 신청 시 직접 지정한 인증키(OC) 값을 입력하세요.",
    )
    st.markdown("---")
    st.markdown(
        "**OC 발급 방법**\n"
        "1. [law.go.kr](https://www.law.go.kr) 회원가입\n"
        "2. OPEN API 신청 메뉴에서 활용신청\n"
        "3. 원하는 인증키(OC) 값을 직접 입력 후 신청 (자동승인)"
    )
    st.markdown("---")
    st.caption("💡 이 도구는 참고용 자동 체크이며, "
               "최종 인용 정확성은 law.go.kr 원문으로 재확인하시기 바랍니다.")

if not oc:
    st.info("👈 왼쪽 사이드바에 API 인증키(OC)를 입력하면 조회를 시작할 수 있습니다.")

tab0, tab1, tab2 = st.tabs([
    "📄 제안요청서 파일 검사 (권장)",
    "📝 법령명 직접 입력",
    "📊 엑셀로 여러 건 조회",
])

# ---- 탭 0: 제안요청서 파일 업로드 → 법령명 자동 추출 → 일괄 검사 ----
with tab0:
    st.subheader("제안요청서 파일을 올리면 법령명을 자동으로 찾아 검사합니다")
    st.caption(
        "문서 안의 법령·지침명을 자동으로 추출한 뒤, 국가법령정보센터와 대조하여 "
        "**미현행(폐지·명칭변경)된 항목**을 찾아냅니다."
    )

    doc_file = st.file_uploader(
        "제안요청서 파일 업로드",
        type=["hwp", "hwpx", "docx", "txt"],
        help="한글(hwp/hwpx), 워드(docx), 텍스트(txt) 파일을 지원합니다.",
        key="doc_upload",
    )

    with st.expander("📋 파일 없이 본문을 붙여넣어 검사하기"):
        pasted_text = st.text_area(
            "제안요청서의 관련 법령 부분을 복사해서 붙여넣으세요",
            height=180,
            placeholder="예:\n1. 「개인정보 보호법」\n2. 「전자정부법」 제45조\n3. 「국가정보화 기본법」",
            key="pasted_text",
        )

    doc_text = None
    read_error = None

    if doc_file is not None:
        with st.spinner("문서에서 텍스트를 추출하는 중..."):
            doc_text, read_error = read_document_text(doc_file)
        if read_error:
            st.error(read_error)
    elif pasted_text and pasted_text.strip():
        doc_text = pasted_text

    if doc_text:
        found_laws = extract_law_names(doc_text)

        if not found_laws:
            st.warning(
                "문서에서 법령명을 찾지 못했습니다. "
                "법령명이 「 」 표기 없이 문장 속에 섞여 있으면 인식이 어려울 수 있습니다. "
                "아래 '법령명 직접 입력' 탭을 이용하거나, 관련 법령 부분만 붙여넣어 보세요."
            )
        else:
            st.success(f"문서에서 법령·지침 {len(found_laws)}건을 찾았습니다.")

            # 사용자가 추출 결과를 확인/수정할 수 있게 표시
            edited = st.data_editor(
                pd.DataFrame({"검사대상 법령·지침명": found_laws}),
                use_container_width=True,
                num_rows="dynamic",
                key="extracted_editor",
            )

            st.caption("💡 잘못 추출된 항목은 위 표에서 직접 지우거나 고칠 수 있습니다.")

            if st.button("🔍 전체 검사 시작", type="primary", disabled=not oc, key="doc_check_btn"):
                targets = [
                    str(v).strip() for v in edited["검사대상 법령·지침명"].tolist()
                    if str(v).strip() and str(v).strip().lower() != "nan"
                ]
                results = []
                progress = st.progress(0, text="검사 준비 중...")
                for i, name in enumerate(targets):
                    progress.progress((i + 1) / len(targets), text=f"검사 중: {name}")
                    r = check_one(oc, "법령", name)
                    official_name = r.get("name") or ""
                    is_match = official_name and normalize_name(official_name) == normalize_name(name)
                    similar = r.get("similar_candidates") or []

                    def _fmt_similar(cands, limit=3):
                        parts = []
                        for c in cands[:limit]:
                            date = format_date(c.get("enforce_date"))
                            piece = f"{c['name']} [{c['gubun']}"
                            if date:
                                piece += f", 시행 {date}"
                            piece += f", 유사도 {c['similarity']}%]"
                            parts.append(piece)
                        return " / ".join(parts)

                    if is_match and r.get("auto_corrected_from"):
                        status = "✅ 현행"
                        note = f"{r.get('auto_corrected_to')}(으)로 확인됨"
                    elif is_match:
                        status = "✅ 현행"
                        note = ""
                    elif similar:
                        # 정확히 일치하진 않지만 비슷한 것이 있음 -> 담당자 판단용 후보 제시
                        status = "❗ 미현행 의심"
                        note = "정확히 일치하는 법령이 없습니다. 유사 후보: " + _fmt_similar(similar)
                    elif r.get("error"):
                        status = "⚠️ 확인필요"
                        note = (
                            "국가법령정보센터에서 찾지 못했습니다. "
                            "폐지되었거나 명칭이 바뀐 법령이거나, 법령명이 아닐 수 있습니다."
                        )
                    else:
                        status = "❗ 미현행 의심"
                        note = f"문서의 명칭과 다릅니다. 현재 정식명칭: {official_name}"

                    results.append({
                        "문서에 적힌 명칭": name,
                        "상태": status,
                        "현행 정식명칭": official_name,
                        "최신 시행일자": format_date(r.get("enforce_date")),
                        "소관부처": r.get("dept") or "",
                        "조치 안내": note,
                    })
                    time.sleep(0.3)
                progress.empty()

                result_df = pd.DataFrame(results)

                # 요약
                n_ok = sum(1 for r in results if r["상태"] == "✅ 현행")
                n_warn = sum(1 for r in results if r["상태"] == "❗ 미현행 의심")
                n_check = sum(1 for r in results if r["상태"] == "⚠️ 확인필요")

                c1, c2, c3 = st.columns(3)
                c1.metric("✅ 현행", f"{n_ok}건")
                c2.metric("❗ 미현행 의심", f"{n_warn}건")
                c3.metric("⚠️ 확인필요", f"{n_check}건")

                if n_warn or n_check:
                    st.warning(
                        f"수정이 필요할 수 있는 항목이 {n_warn + n_check}건 있습니다. "
                        "아래 표의 '조치 안내'를 확인하세요."
                    )
                else:
                    st.success("모든 법령·지침이 현행 명칭과 일치합니다.")

                def highlight_status(row):
                    if row["상태"] == "✅ 현행":
                        return ["background-color: #C6EFCE"] * len(row)
                    elif row["상태"] == "❗ 미현행 의심":
                        return ["background-color: #FFC7CE"] * len(row)
                    else:
                        return ["background-color: #FFEB9C"] * len(row)

                st.dataframe(
                    result_df.style.apply(highlight_status, axis=1),
                    use_container_width=True,
                )

                st.download_button(
                    "📥 검사 결과 CSV 다운로드",
                    data=result_df.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"제안요청서_법령검사결과_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                )

# ---- 탭 1: 단건 직접 입력 조회 ----
with tab1:
    st.subheader("법령명을 입력하면 바로 조회합니다")
    col1, col2 = st.columns([3, 1])
    with col1:
        query_name = st.text_input("법령·지침명", placeholder="예: 개인정보 보호법", key="single_query")
    with col2:
        gubun = st.selectbox("구분", ["법령", "행정규칙", "자치법규"], key="single_gubun")

    if st.button("🔍 조회하기", type="primary", disabled=not oc):
        if not query_name.strip():
            st.warning("법령·지침명을 입력해주세요.")
        else:
            with st.spinner(f"'{query_name}' 조회 중..."):
                result = check_one(oc, gubun, query_name.strip())

            similar = result.get("similar_candidates") or []

            if result.get("error") and not similar:
                st.error(f"❌ 확인필요: {result['error']}")
                st.caption("법령명을 다시 확인하시거나, law.go.kr에서 직접 검색해보세요.")
            else:
                official_name = result.get("name") or ""
                is_match = normalize_name(official_name) == normalize_name(query_name)
                corrected_from = result.get("auto_corrected_from")
                corrected_to = result.get("auto_corrected_to")

                if is_match and not corrected_from:
                    st.success(f"✅ 일치: 입력하신 명칭이 정식 명칭과 동일합니다.")
                elif is_match and corrected_from:
                    st.info(
                        f"🔵 일치 (구분 자동보정): '{corrected_from}'(으)로는 찾지 못해 "
                        f"'{corrected_to}'(으)로 재검색하여 일치를 확인했습니다."
                    )
                else:
                    st.warning("⚠️ 정확히 일치하는 법령을 찾지 못했습니다.")

                if is_match:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("조회된 정식명칭", official_name or "-")
                    c2.metric("공포일자", format_date(result.get("proclaim_date")) or "-")
                    c3.metric("최신 시행일자", format_date(result.get("enforce_date")) or "-")
                    c4.metric("소관부처", result.get("dept") or "-")

                if similar:
                    st.markdown("**혹시 이것을 찾으셨나요? (유사 후보)**")
                    sim_df = pd.DataFrame([
                        {
                            "정식명칭": c["name"],
                            "구분": c["gubun"],
                            "최신 시행일자": format_date(c.get("enforce_date")),
                            "소관부처": c.get("dept") or "",
                            "유사도": f"{c['similarity']}%",
                        }
                        for c in similar
                    ])
                    st.dataframe(sim_df, use_container_width=True, hide_index=True)
                    st.caption(
                        "💡 문서에 적힌 명칭이 약칭이거나 앞부분(기관명 등)이 빠진 경우 "
                        "이렇게 나타납니다. 위 정식명칭으로 수정하시면 됩니다."
                    )

# ---- 탭 2: 엑셀 업로드로 일괄 조회 ----
with tab2:
    st.subheader("여러 법령을 한번에 조회합니다")
    st.caption("엑셀(.xlsx) 파일에 '법령명', '구분'(선택) 컬럼이 있으면 업로드해서 일괄 조회할 수 있습니다.")

    sample_df = pd.DataFrame({
        "구분": ["법령", "법령", "행정규칙"],
        "법령명": ["개인정보 보호법", "전자정부법", "지방자치단체를 당사자로 하는 계약에 관한 법률 시행령"],
    })
    st.download_button(
        "📥 샘플 엑셀 양식 다운로드",
        data=sample_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="법령목록_샘플.csv",
        mime="text/csv",
    )

    uploaded = st.file_uploader("엑셀 또는 CSV 파일 업로드", type=["xlsx", "csv"])

    if uploaded is not None:
        try:
            if uploaded.name.endswith(".csv"):
                df = pd.read_csv(uploaded)
            else:
                df = pd.read_excel(uploaded)
        except Exception as e:
            st.error(f"파일을 읽지 못했습니다: {e}")
            df = None

        if df is not None:
            name_col = None
            for cand in ["법령명", "법령·지침명", "법령지침명"]:
                if cand in df.columns:
                    name_col = cand
                    break
            gubun_col = "구분" if "구분" in df.columns else None

            if name_col is None:
                st.error("'법령명' 컬럼을 찾을 수 없습니다. 샘플 양식을 참고해주세요.")
            else:
                st.dataframe(df, use_container_width=True)
                if st.button("🔍 전체 일괄 조회", type="primary", disabled=not oc):
                    results = []
                    progress = st.progress(0, text="조회 준비 중...")
                    rows = df.to_dict("records")
                    for i, row in enumerate(rows):
                        name = str(row.get(name_col, "")).strip()
                        gubun_val = str(row.get(gubun_col, "법령")).strip() if gubun_col else "법령"
                        if not name or name == "nan":
                            continue
                        progress.progress((i + 1) / len(rows), text=f"조회 중: {name}")
                        r = check_one(oc, gubun_val, name)
                        official_name = r.get("name") or ""
                        is_match = official_name and normalize_name(official_name) == normalize_name(name)
                        if r.get("error"):
                            status = "확인필요"
                        elif is_match and r.get("auto_corrected_from"):
                            status = "일치(구분보정)"
                        elif is_match:
                            status = "일치"
                        else:
                            status = "불일치"
                        results.append({
                            "입력명": name,
                            "입력구분": gubun_val,
                            "조회된정식명칭": official_name,
                            "공포일자": format_date(r.get("proclaim_date")),
                            "최신시행일자": format_date(r.get("enforce_date")),
                            "소관부처": r.get("dept") or "",
                            "일치여부": status,
                            "비고": r.get("error", "") or "",
                        })
                        time.sleep(0.3)
                    progress.empty()

                    result_df = pd.DataFrame(results)

                    def highlight(row):
                        if row["일치여부"] == "일치":
                            return ["background-color: #C6EFCE"] * len(row)
                        elif row["일치여부"] == "일치(구분보정)":
                            return ["background-color: #BDD7EE"] * len(row)
                        elif row["일치여부"] == "불일치":
                            return ["background-color: #FFC7CE"] * len(row)
                        else:
                            return ["background-color: #FFEB9C"] * len(row)

                    st.dataframe(result_df.style.apply(highlight, axis=1), use_container_width=True)

                    csv_data = result_df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 결과 CSV 다운로드", data=csv_data,
                        file_name=f"법령체크결과_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                    )

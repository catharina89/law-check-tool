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

    first_item = None
    for tag in ["law", "admrul", "ordin"]:
        items = root.findall(f".//{tag}")
        if items:
            first_item = items[0]
            break
    if first_item is None:
        children = list(root)
        if children:
            for child in children[1:]:
                if list(child):
                    first_item = child
                    break
    if first_item is None:
        return {"error": "응답 구조를 해석하지 못함"}

    name = find_first_text(first_item, ["법령명한글", "행정규칙명", "자치법규명", "법령명"])
    proclaim_date = find_first_text(first_item, ["공포일자"])
    enforce_date = find_first_text(first_item, ["시행일자"])
    dept = find_first_text(first_item, ["소관부처명", "소관부처"])

    return {
        "name": name, "proclaim_date": proclaim_date,
        "enforce_date": enforce_date, "dept": dept, "error": None,
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


def _search_one_target(oc, target, query_name):
    params = {"OC": oc, "target": target, "type": "XML", "query": query_name, "display": 5}
    try:
        raw = http_get(SEARCH_URL, params)
    except Exception as e:
        return {"error": f"네트워크 오류: {e}"}
    return parse_search_result(raw)


def check_one(oc, gubun, query_name):
    """지정 구분으로 우선 검색, 불일치/실패 시 다른 구분으로 자동 재시도."""
    primary_target = TARGET_MAP.get(gubun, "law")
    primary_result = _search_one_target(oc, primary_target, query_name)
    primary_ok = (
        not primary_result.get("error")
        and primary_result.get("name")
        and normalize_name(primary_result["name"]) == normalize_name(query_name)
    )
    if primary_ok:
        primary_result["matched_target"] = primary_target
        return primary_result

    other_targets = [t for t in ("law", "admrul", "ordin") if t != primary_target]
    for alt_target in other_targets:
        alt_result = _search_one_target(oc, alt_target, query_name)
        alt_ok = (
            not alt_result.get("error")
            and alt_result.get("name")
            and normalize_name(alt_result["name"]) == normalize_name(query_name)
        )
        if alt_ok:
            alt_result["matched_target"] = alt_target
            alt_result["auto_corrected_from"] = GUBUN_LABELS.get(primary_target, primary_target)
            alt_result["auto_corrected_to"] = GUBUN_LABELS.get(alt_target, alt_target)
            return alt_result

    primary_result["matched_target"] = primary_target
    return primary_result


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

tab1, tab2 = st.tabs(["📝 직접 입력해서 조회", "📊 여러 건 한번에 조회 (엑셀 업로드)"])

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

            if result.get("error"):
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
                    st.warning("⚠️ 불일치: 입력명과 조회된 정식명칭이 다릅니다.")

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("조회된 정식명칭", official_name or "-")
                c2.metric("공포일자", format_date(result.get("proclaim_date")) or "-")
                c3.metric("최신 시행일자", format_date(result.get("enforce_date")) or "-")
                c4.metric("소관부처", result.get("dept") or "-")

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

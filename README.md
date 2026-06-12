# 지직 (zzk)

**치지직 오픈소스 로컬 웹 방송 다운로더**

- 웹 UI
- 채널 등록 → 방송 시작 자동 감지 → 자동 녹화
- **중단 안전**: 어떤 이유로든 녹화가 끊겨도 `.ts` 세그먼트 + `.m3u8` 로 마지막까지 재생 가능
- 완전 로컬 & 오픈소스

## 빠른 시작

```bash
# 1. 의존성 설치 (uv 추천)
uv sync

# 2. 서버 실행
uv run zzk
```

브라우저에서 http://127.0.0.1:8001 접속

## 사용 방법

1. **채널 등록**
   - 채널 URL 또는 채널 ID 입력 (예: `affa78deac0b23d2046b8ed4856c1e62`)
   - 화질 선택 (best / 1080p / 720p ...)

2. **자동 녹화**
   - "자동 녹화" 체크 시 백그라운드 모니터가 주기적으로 라이브 상태를 확인
   - 방송이 `OPEN` 되면 즉시 HLS 세그먼트 다운로드 시작

 3. **녹화 파일 구조** (예시)
    ```
    recordings/
    └── 채널명/
        └── 2026-06-12/
            └── 방송제목/
                ├── 방송제목.m3u8     ← 이 세션 전체 재생
                ├── recording.json
                └── chunk/
                    └── segment_00000.ts ...
    ```
    같은 날 여러 방송/재접속 시 자동으로 `방송제목/`, `방송제목_HHMMSS/`, ... 로 분리 저장.
    - **중간에 꺼져도 재생 가능**: m3u8 + chunk/ 세그먼트 그대로 VLC/mpv/PotPlayer로 열기
   - 웹 UI 안에서도 "재생" 버튼으로 브라우저 내 HLS 재생 가능 (hls.js)

4. **분할 저장의 장점**
   - 파일 크기 관리 용이
   - 업로드/백업 편리
   - `playlist.m3u8` 하나로 모든 chunk를 연속 재생 가능

## 기술적 특징 (요구사항 만족)

- **오픈소스 + 로컬호스팅 (웹)**: FastAPI + 순수 HTML/JS (Tailwind CDN)
- **자동 대기 + 자동 시작**: 등록된 모든 채널을 45초 주기로 폴링
- **복원력 있는 녹화**:
  - 세그먼트 단위로 즉시 디스크 기록
  - `.m3u8` 실시간 갱신 + flush
  - 프로세스 강제 종료 시에도 기존 `.ts` + 마지막 플레이리스트로 재생 가능
- **.ts + .m3u8 분할**:
  - `segment_minutes` 설정 시 wall-clock 기준으로 chunk 회전
  - 각 chunk는 독립 재생 가능하면서 root `playlist.m3u8` 로 전체 연결

## API (주요)

- `GET /api/channels`
- `POST /api/channels` — `{url_or_id, quality, segment_minutes, auto_record}`
- `POST /api/channels/{id}/record` — 수동 즉시 녹화
- `POST /api/channels/{id}/stop`
- `GET /api/recordings`
- `GET /recordings/.../playlist.m3u8` — 직접 재생 URL

## 주의사항

- 일부 로그인 전용/연령 제한 방송은 쿠키가 필요할 수 있음
  - streamlink 설정 파일(`~/.config/streamlink/config` 또는 Windows `%APPDATA%\streamlink\config`)에 쿠키를 지정하면 자동으로 적용됩니다.
  - 예: `http-cookies=NID_AUT=...;NID_SES=...`
- streamlink의 chzzk 플러그인이 스트림 URL 해석을 담당하므로, 대부분의 CHZZK 변화에 자동 대응됩니다.
- 장시간 녹화 시 토큰 만료를 대비해 5분마다 live-detail을 재조회 + streamlink 재해석
- 저장 공간 충분히 확보하세요 (1080p60은 시간당 수 GB)

## 개발 / 기여

```bash
uv run uvicorn app.main:app --reload
```

주요 소스:
- `app/chzzk.py` — API 클라이언트 (메타데이터/상태) + **streamlink 전용 스트림 URL 해석기**
- `app/recorder.py` — 핵심 세그먼트 다운로더 (복원력 + 분할 로직). **streamlink only**로 HLS URL 해석
- `app/db.py` — SQLite (채널 + 녹화 이력)
- `app/main.py` — FastAPI + 모니터 루프
- `app/templates/index.html` — 단일 파일 웹 UI

**streamlink only**: 방송 재생 URL 추출은 [streamlink](https://streamlink.github.io/)의 공식 `chzzk` 플러그인만 사용합니다. 커스텀 HLS 스크래핑은 더 이상 사용되지 않습니다. (쿠키 설정으로 제한 방송 지원, 60fps 품질 등 개선)

## 라이선스

GPL-3.0 [LICENSE](LICENSE)

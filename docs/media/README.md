# deident 소개영상

연구자·실무자를 위한 30초 한국어 모션그래픽입니다. 무음으로 제작했으며, 핵심 내용을 화면에 표시합니다.
정책한걸음 최종 로고에서 추출한 파랑·남색·회색, 연결 곡선과 굵은 제목을 적용했습니다.
[디자인 기준](../../DESIGN.md) · [브랜드 보드](brand-board.png) · [색상 토큰](brand-tokens.json)

공개 문서·코드의 사용 흐름을 도식화했습니다. 실제 UI나 AI 실행 화면을 녹화한 영상은 아닙니다.

- [MP4 영상](intro.mp4): 1920×1080, 30fps, H.264, 30초
- [README 미리보기 GIF](intro-preview.gif): 처리·결과 장면 9초, 960×540, 10fps
- [정지 포스터](intro-poster.png) · [5개 장면 전체](intro-storyboard.png)
- [내용 데이터](intro.json) · [렌더 스크립트](render_intro.py)

## 영상 대본

| 시간 | 전달할 내용 |
|---|---|
| 0–4초 | 문서를 공유하기 전, 민감정보부터 살핍니다. — 로컬에서 탐지·가명화·재검사 |
| 4–11초 | 입력: 처리할 로컬 문서 + 예외·강제탐지 사전. 연구계획서·명단 등 문서를 공유하기 전에 탐지 가능한 민감정보를 처리할 때 |
| 11–20초 | guard 점검 → scan 탐지 → apply 치환 → 검증·격리. 결과: 게이트 통과 산출물 + 마스킹 리포트 |
| 20–26초 | 탐지·치환은 로컬 코드로 |
| 26–30초 | 샘플 문서에 guard부터 실행하세요 |

요청·사용 예시:

```text
guard로 먼저 위험을 확인하고,
scan으로 탐지 결과를 살핀 뒤
apply로 비식별본을 만듭니다.
```

강점 장면의 자막:

- **같은 값, 같은 가명**: 문서 안의 동일 인물을 일관되게
- **원값 매핑 분리**: 민감한 매핑은 _private/에 보관
- **불합격 결과 격리**: 검증 게이트 통과분만 out/으로

사용 범위: 탐지 누락·이미지 속 글자·문맥 재식별은 남을 수 있습니다. 사람의 확인이 필요합니다.

## 설명의 근거와 확인 범위

기준 소스 커밋: `1cbdeb4cdd0af9eebf16bd0982eb24d2290bdcff`. 영상 길이는 작업 소요시간이나 성능 수치를 뜻하지 않습니다.

- [run.py](../../run.py)
- [deident/cli.py](../../deident/cli.py)
- [README.md](../../README.md)

2026-10-10에 가상 이름·기관을 사용자 사전에 명시한 Markdown 문서로 apply를 실행했습니다. 탐지 3건(성명 2·기관 1), 재탐지 0, 원값 잔존 0으로 게이트를 통과했습니다. 같은 이름이 [연구자A]로 일관되게 치환됐습니다. 이 확인은 치환·검증 흐름의 예시이며 자동 탐지 재현율 평가가 아닙니다.

## 다시 만들기

Python 3.9 이상, Pillow, FFmpeg, 한국어 글꼴이 필요합니다. 이 의존성은 영상 재생이나 도구 사용에는 필요하지 않고 **영상 재생성에만** 쓰입니다.

```bash
python3 -m pip install Pillow
# FFmpeg는 운영체제의 패키지 관리자로 설치합니다.
python3 docs/media/render_intro.py
```

설명용 기본 글꼴은 macOS의 Pretendard ExtraBold·Regular이며 Apple SD Gothic Neo 또는 Linux의 Noto Sans CJK로 대체할 수 있습니다. 로고는 서체로 재조판하지 않고 원본 PNG를 사용합니다.
다른 환경에서는 한국어 글꼴 경로를 지정합니다. 글꼴 파일은 저장소에 포함하지 않습니다.

```bash
INTRO_FONT=/path/to/Regular.otf INTRO_FONT_BOLD=/path/to/Bold.otf python3 docs/media/render_intro.py
```

`brand-logo.png`는 제공된 원본 PNG와 체크섬이 같은 파일입니다. 투명 바깥 여백은 배치할 때만 제외합니다. `brand-tokens.json`에 원본에서 확인한 RGB 값과 화면용 파생색을 구분해 두었습니다.

`intro.json`의 문구를 고친 뒤 렌더링하면 MP4·GIF·포스터·스토리보드를 함께 갱신합니다.
`--stills`는 정지 이미지만, `--fps 24`는 다른 프레임률의 MP4를 만듭니다(그 경우 위 사양도 수정하세요).
내용이 바뀌면 이 대본과 README의 활용 예시도 함께 맞춥니다.

README에는 GIF를 이미지로 넣고 MP4 파일로 연결했습니다. 정지 이미지와 이 대본으로도 같은 내용을 확인할 수 있습니다.

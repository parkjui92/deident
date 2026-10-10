# deident 소개영상

30초 한국어 사용 예시. 로고는 1920px 화면에서 너비 144px로 줄이고, 실제 사용하는 터미널·요청·결과 파일에 화면을 배분했습니다.

가상 문서로 실제 실행한 명령·출력을 재생한 30초 영상입니다. 이름·기관을 사전에 등록한 예제입니다.

실행 원문은 [demo-run.json](demo-run.json)에 저장했습니다. `examples/intro-demo.md`와 일치하는 가상 문서만 사용했습니다. 이름·기관을 사전에 넣었으므로 자동 탐지 재현율의 평가가 아닙니다. 화면에서는 개인정보나 실제 업무 자료를 사용하지 않았습니다.

## 영상 구성

| 구간 | 화면 | 읽을 내용 |
|---|---|---|
| 0–7초 | 공유할 문서에서 이름·기관을 먼저 찾습니다 | 이 예제는 가상 이름·기관을 사전에 등록하고 실행했습니다. |
| 7–14초 | apply를 실행하면 치환 후 재검사합니다 | 이 실행에서는 탐지 3건을 처리하고 검증 게이트를 통과했습니다. |
| 14–23초 | 두 번 나온 이름은 같은 가명으로 바뀝니다 | 직함은 남고, 같은 인물은 문서 안에서 계속 같은 가명을 씁니다. |
| 23–30초 | 비식별본과 원값 매핑은 따로 보관합니다 | out/의 결과를 직접 확인한 뒤 공유합니다. |

## 파일·근거

- [MP4](intro.mp4): 1920×1080, 30fps, 30초, 무음 H.264
- [README GIF](intro-preview.gif): 같은 30초 전체, 960×540, 8fps
- [포스터](intro-poster.png) · [4개 장면](intro-storyboard.png)
- [대본·화면 데이터](intro.json) · [렌더러](render_intro.py)

기준 소스 커밋: `accce434199af26de49c9ddf20e0f9c68c01e71f`. 영상 길이는 실제 처리시간을 뜻하지 않습니다.

- [deident/cli.py](../../deident/cli.py)
- [deident/report/summary.py](../../deident/report/summary.py)
- [examples/intro-demo.md](../../examples/intro-demo.md)
- [docs/media/demo-run.json](../../docs/media/demo-run.json)

## 다시 만들기

Python 3.9+, Pillow, FFmpeg, 한국어 글꼴이 필요합니다. 이 의존성은 영상 재생성에만 쓰입니다.

```bash
python3 -m pip install Pillow
python3 docs/media/render_intro.py
# 정지 이미지 먼저 확인
python3 docs/media/render_intro.py --stills
```

기본 글꼴은 Pretendard이며 Apple SD Gothic Neo 또는 Noto Sans CJK를 대체로 사용합니다. `INTRO_FONT`, `INTRO_FONT_BOLD` 환경변수로 글꼴 경로를 지정할 수 있습니다. `intro.json`의 화면 문구를 바꾼 뒤 다시 렌더링하면 MP4·GIF·포스터·스토리보드를 갱신합니다. 원본 로고 PNG는 수정하지 않으며 렌더링 전에 체크섬을 확인합니다.

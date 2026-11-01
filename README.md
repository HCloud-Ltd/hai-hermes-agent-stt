# hai-hermes-agent-stt

[Hermes Agent](https://hermes-agent.nousresearch.com/) 用の **HAI 音声文字起こし (STT) バックエンドプラグイン** です。
[HAI](https://hai.hcloud.ltd) の OpenAI 互換 Audio API (`/v1/audio/transcriptions`) 経由で
**Whisper Large V3 Turbo** (`whisper-large-v3-turbo`, 自社DC) / **Whisper Large V3** を利用できます。

- Telegram / Discord などのゲートウェイに届いた音声メッセージの文字起こし
- CLI の音声入力 (`voice.record_key`) の文字起こし
- API キーは **環境変数 `HAI_API_KEY`** から取得 (`.env` への自動書き込みはしない)

> HAI は [OpenAI 互換](https://hai.hcloud.ltd/docs/transcriptions) で、`/v1/audio/transcriptions` は
> OpenAI SDK の `audio.transcriptions.create` とそのまま互換です。本プラグインはそれを
> Hermes の STT プロバイダーとして差し込めるように包んだものです。

---

## 仕組み

Hermes の音声文字起こしは**内蔵**の `transcribe_audio` が行います。文字起こしの「実体」は
**プラグイン単位で差し替え可能**なプロバイダーとして実装されており、このプラグインは
その 1 つ (`stt.provider: hai`) です。

```
transcribe_audio (Hermes 内蔵)
        │
        └─ stt.provider の設定でプロバイダー選択
               ├─ (内蔵) local / groq / openai / mistral / xai / elevenlabs / deepinfra
               └─ (このプラグイン) hai  ← HAI / whisper-large-v3-turbo
```

### なぜ内蔵の `openai` プロバイダーに `base_url` を向けるだけでは駄目か

内蔵 `openai` プロバイダーは、モデル名が Groq 系 (`whisper-large-v3-turbo` を含む) だと
「OpenAI には無いモデル」と判断して **黙って `whisper-1` に書き換えます**。HAI は `whisper-1`
を知らないので 400 になります。環境変数 `STT_OPENAI_MODEL` で既定値ごと差し替えれば
回避できますが、Hermes の内部実装に依存した回避策になるため、公開プラグイン API で
独立したプロバイダーにしています。

### 依存

- Hermes Agent (本プラグインは `agent.transcription_provider` の公開 API にのみ依存)
- Python 標準ライブラリ + `openai` (Hermes に同梱)

---

## インストール

### 事前準備

1. **HAI API キー** を取得 ([hai.hcloud.ltd](https://hai.hcloud.ltd))。
   文字起こしは**有料キー** (`hai_...`) が必要で、無料トライアルキーでは
   `/v1/audio/transcriptions` を呼べません。
2. キーを**環境変数 `HAI_API_KEY`** に設定します。

   ```bash
   export HAI_API_KEY=hai_xxxxxxxxxxxx
   ```

   持続させる場合、Hermes の `.env` (`~/.hermes/.env`、またはアクティブ
   プロファイルの `.env`) に `HAI_API_KEY=hai_...` を追記してください。
   本プラグインは `.env` を自動で書き込まず、環境変数から読む想定です。

### プラグインの取得

```bash
hermes plugins install HCloud-Ltd/hai-hermes-agent-stt
```

インストール時に「有効化しますか?」と聞かれるので `y` (あるいは `--enable` 付きで
非対話)。

### 設定

`config.yaml` (または `hermes config set`) に以下を追加します。

```yaml
stt:
  provider: hai
  hai:
    model: whisper-large-v3-turbo        # 任意 (既定)
    language: ja                         # 任意。空なら音声から自動判定
    # base_url: https://hai-api.hcloud.ltd/v1   # 任意 (既定)
    # timeout: 120                              # 任意 (秒)
```

`hermes config set` 版:

```bash
hermes config set stt.provider hai
hermes config set stt.hai.model whisper-large-v3-turbo
hermes config set stt.hai.language ja
```

> `stt.language` (全プロバイダー共通のヒント) が `en` のままだと、`stt.hai.language` を
> 空にしても英語固定になります。自動判定にしたい場合は両方を空にしてください。

> ユーザープラグインは**オプトイン**です。`plugins.enabled` に
> `hai-stt` を含める必要があります (インストール時の確認で `y` を
> 選べば自動で書かれます)。手動で:
>
> ```bash
> hermes plugins enable hai-stt
> ```

### 反映

ゲートウェイは起動時にプラグインを読み込みます。`stt.provider` の変更は次の
文字起こしから反映されますが、プラグインの新規インストール後はゲートウェイを
再起動してください。

---

## 使い方

Telegram / Discord などで音声メッセージを送るだけです。Hermes が `stt.provider: hai`
を検出して HAI にルーティングし、文字起こし結果をユーザー発話として扱います
(`stt.echo_transcripts: true` なら 🎙️ 付きで原文も返します)。

### OpenAI SDK で直接 (参照用)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://hai-api.hcloud.ltd/v1",
    api_key="hai_...",   # HAI_API_KEY
)

with open("meeting.mp3", "rb") as f:
    resp = client.audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=f,
        language="ja",  # 省略すると自動判定
    )
print(resp.text)
```

---

## 設定項目

| 場所 | キー | 既定 | 説明 |
|------|------|------|------|
| 環境変数 | `HAI_API_KEY` | (必須) | HAI API キー (`hai_...`) |
| `config.yaml` | `stt.provider` | — | `hai` に設定して有効化 |
| `config.yaml` | `stt.hai.model` | `whisper-large-v3-turbo` | 利用するモデル ID (`whisper-large-v3` も可) |
| `config.yaml` | `stt.hai.language` | (空 = 自動判定) | ISO-639-1 の言語ヒント (`ja` など) |
| `config.yaml` | `stt.hai.base_url` | `https://hai-api.hcloud.ltd/v1` | API ベース URL |
| `config.yaml` | `stt.hai.timeout` | `120` | リクエストのタイムアウト (秒) |
| `config.yaml` | `stt.prompt` | (空) | 語彙ヒント (Hermes 共通設定。2,000 文字で末尾を残して切り詰め) |
| 環境変数 | `HAI_STT_BASE_URL` | (同上) | base_url の環境変数上書き |
| 環境変数 | `HAI_STT_MODEL` | (model の上) | モデル ID の環境変数上書き |

---

## 課金・制限

- 課金は**音声の長さ (秒)** です。1 秒未満は切り上げ、最低 1 秒
  (turbo ¥0.025/秒 = ¥1.5/分、v3 ¥0.03/秒 = ¥1.8/分)。トークン数には依存しません。
- ファイル上限は 25 MB (128 kbps MP3 で約 25 分)。Hermes 側も同じ 25 MB で事前に弾きます。
- 受け付ける形式は mp3 / mp4 / m4a / wav / flac / ogg / webm。Hermes が Telegram の
  音声メッセージ (ogg/opus) をそのまま送ります。
- ストリーミング非対応。同期で返るので長い音声ほど待ち時間が伸びます。
- 定額プランの対象外で、クレジットから消費します。

詳細は [HAI ドキュメント](https://hai.hcloud.ltd/docs/transcriptions) を参照してください。

---

## トラブルシューティング

| 症状 | 原因 / 対処 |
|------|------------|
| `HAI_API_KEY が設定されていません` | 環境変数にキーがない。`export HAI_API_KEY=hai_...` または `.env` に追記 |
| `HAI が API キーを拒否しました (HTTP 401/403)` | トライアルキーまたは無効キー。有料キー (`hai_...`) を使用 |
| `HAI 文字起こしに失敗しました (HTTP 400): Unknown model` | `stt.hai.model` が HAI のカタログに無い。`whisper-large-v3-turbo` / `whisper-large-v3` のどちらかにする |
| `stt.provider='hai' is set but no built-in, command, or plugin provider registered that name` | プラグインがインストール / 有効化されていない。`hermes plugins list` で確認し、`hermes plugins enable hai-stt` を実行 |
| 英語で文字起こしされる | `stt.language: en` が残っている。`stt.hai.language: ja` を設定するか `stt.language` を空にする |

---

## 免責

本プラグインは HAI の API を利用します。利用条件・料金・可用性は
[HAI](https://hai.hcloud.ltd) の各ページに準じます。

## ライセンス

[MIT](./LICENSE)

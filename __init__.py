"""HAI (https://hai.hcloud.ltd) 音声文字起こし (STT) バックエンド。

HAI の OpenAI 互換 ``/v1/audio/transcriptions`` 表面で ``whisper-large-v3-turbo``
(Whisper Large V3 Turbo, 自社DC) / ``whisper-large-v3`` を利用します。
Telegram / Discord などのゲートウェイに届いた音声メッセージと、CLI の音声入力の
文字起こしに使われます。

このプラグインは Hermes の公開プラグイン API (``agent.transcription_provider``)
と ``openai`` SDK にのみ依存します。Hermes 内部の非公開モジュールには触れず、
バージョンアップに強くしています。

設定
----
``config.yaml``::

    stt:
      provider: hai
      hai:
        model: whisper-large-v3-turbo             # 任意 (既定)
        language: ja                              # 任意。空なら自動判定
        # base_url: https://hai-api.hcloud.ltd/v1 # 任意 (既定)
        # timeout: 120                            # 任意 (秒)

認証キーは **環境変数 ``HAI_API_KEY``** から取得します (有料キー ``hai_...``。
トライアルキーは文字起こしエンドポイントで使えません)。``.env`` への自動書き込み
は行わず、環境変数から注入する想定です。

ドキュメント: https://hai.hcloud.ltd/docs/transcriptions
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret
from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

# --- 定数 -------------------------------------------------------------------
PROVIDER_NAME = "hai"
DEFAULT_BASE_URL = "https://hai-api.hcloud.ltd/v1"
API_KEY_ENV = "HAI_API_KEY"
BASE_URL_ENV = "HAI_STT_BASE_URL"
MODEL_ENV = "HAI_STT_MODEL"
# 長い音声ほど同期で待つ。HAI 側は 25 MB 上限 (128 kbps MP3 で約 25 分)。
DEFAULT_TIMEOUT_SECONDS = 120.0
# HAI の prompt 上限 (超えると 400)。Whisper 自体は末尾 224 トークンしか見ない。
MAX_PROMPT_CHARS = 2_000

# HAI 文字起こしモデルカタログ (GET /v1/audio/models 参照)。ID は
# /v1/audio/transcriptions にそのまま送られる。課金は音声の長さ (秒) 単位。
_MODELS: Dict[str, Dict[str, Any]] = {
    "whisper-large-v3-turbo": {
        "display": "Whisper Large V3 Turbo (HAI)",
        "speed": "速度優先 (デコーダ 4 層)",
        "strengths": "多言語の音声文字起こし。V3 のデコーダを 32 層 → 4 層に刈り込んだ高速版",
        "price": "¥0.025 / 秒 (¥1.5 / 分)",
    },
    "whisper-large-v3": {
        "display": "Whisper Large V3 (HAI)",
        "speed": "精度優先",
        "strengths": "多言語の音声文字起こし。精度優先",
        "price": "¥0.03 / 秒 (¥1.8 / 分)",
    },
}
DEFAULT_MODEL = "whisper-large-v3-turbo"


def _openai_importable() -> bool:
    try:
        import openai  # noqa: F401

        return True
    except ImportError:
        return False


def _resolve_api_key() -> Optional[str]:
    """``HAI_API_KEY`` を取得 (環境変数 / プロファイルの secret scope)。無ければ None。"""
    return get_secret(API_KEY_ENV)


def _stt_section() -> Dict[str, Any]:
    """``config.yaml`` の ``stt.hai`` セクション。読めなければ空 dict。"""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        stt = cfg.get("stt") if isinstance(cfg, dict) else {}
        section = stt.get(PROVIDER_NAME) if isinstance(stt, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _resolve_base_url(section: Dict[str, Any]) -> str:
    """``stt.hai.base_url`` → 環境変数 ``HAI_STT_BASE_URL`` → 既定 URL。"""
    val = section.get("base_url")
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/")
    env = os.environ.get(BASE_URL_ENV, "").strip()
    return env.rstrip("/") or DEFAULT_BASE_URL


def _resolve_model(caller_model: Optional[str], section: Dict[str, Any]) -> str:
    """``model`` kwarg (Hermes が ``stt.hai.model`` を渡してくる) → 環境変数
    ``HAI_STT_MODEL`` → 既定。カタログ外の ID は警告してそのまま送る (HAI が 400 で
    「Available: ...」を返すので、黙って既定に倒すより設定ミスが見える)。"""
    candidates: List[str] = []
    if isinstance(caller_model, str) and caller_model.strip():
        candidates.append(caller_model.strip())
    env = os.environ.get(MODEL_ENV, "").strip()
    if env:
        candidates.append(env)
    val = section.get("model")
    if isinstance(val, str) and val.strip():
        candidates.append(val.strip())
    for candidate in candidates:
        if candidate not in _MODELS:
            logger.warning("HAI STT: model %r is not in the known catalog (%s); sending as-is",
                           candidate, ", ".join(_MODELS))
        return candidate
    return DEFAULT_MODEL


def _resolve_timeout(section: Dict[str, Any]) -> float:
    try:
        val = float(section.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        return val if val > 0 else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _error(message: str, model: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "transcript": "", "error": message, "provider": PROVIDER_NAME}
    if model:
        out["model"] = model
    return out


def _ok(transcript: str, model: str) -> Dict[str, Any]:
    return {"success": True, "transcript": transcript, "provider": PROVIDER_NAME, "model": model}


def _describe_api_error(exc: Exception) -> Tuple[int, str]:
    """OpenAI SDK の例外から ``(HTTP ステータス, メッセージ)`` を取り出す。"""
    status = int(getattr(exc, "status_code", 0) or 0)
    detail = str(getattr(exc, "message", "") or "")
    body = getattr(exc, "body", None)
    # openai SDK は body["error"] を剥がして渡してくる版と剥がさない版がある。
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])
        elif isinstance(err, str):
            detail = err
    return status, detail or str(exc)


class HaiTranscriptionProvider(TranscriptionProvider):
    """HAI ``/v1/audio/transcriptions`` バックエンド (whisper-large-v3-turbo)。"""

    @property
    def name(self) -> str:
        # 設定キー (stt.provider: hai) に一致する識別子。
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "HAI"

    def is_available(self) -> bool:
        # 呼び出し時はキー + SDK のみ確認 (ネットワーク接続しない)。
        return bool(_resolve_api_key()) and _openai_importable()

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        # キーは環境変数注入が前提なので、プロンプト付きの env_vars は出さない。
        return {
            "name": "HAI",
            "badge": "paid",
            "tag": "Whisper Large V3 Turbo via HAI (hai-api.hcloud.ltd) — ¥1.5/分。HAI_API_KEY (環境変数)",
            "env_vars": [],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        section = _stt_section()
        model_id = _resolve_model(model, section)

        api_key = _resolve_api_key()
        if not api_key:
            return _error(
                f"{API_KEY_ENV} が設定されていません。環境変数 HAI_API_KEY に "
                f"有料キー (hai_...) を設定してください (https://hai.hcloud.ltd)。",
                model_id,
            )
        try:
            import openai
        except ImportError:
            return _error("openai パッケージが必要です (pip install openai)。", model_id)

        audio = Path(file_path)
        if not audio.is_file():
            return _error(f"音声ファイルが見つかりません: {file_path}", model_id)

        request: Dict[str, Any] = {"model": model_id, "response_format": "json"}
        if isinstance(language, str) and language.strip():
            request["language"] = language.strip()
        prompt = extra.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            # HAI は 2,000 文字超を 400 で弾く。Whisper は末尾しか見ないので末尾を残す。
            request["prompt"] = prompt.strip()[-MAX_PROMPT_CHARS:]

        client = openai.OpenAI(
            api_key=api_key, base_url=_resolve_base_url(section), timeout=_resolve_timeout(section),
            max_retries=1,
        )
        try:
            with audio.open("rb") as fh:
                response = client.audio.transcriptions.create(file=fh, **request)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HAI transcription failed", exc_info=True)
            status, detail = _describe_api_error(exc)
            suffix = f" (HTTP {status})" if status else ""
            if status in (401, 403):
                return _error(
                    f"HAI が API キーを拒否しました{suffix}: {detail}。"
                    f"トライアルキーは文字起こしで使えません。有料キー (hai_...) を "
                    f"HAI_API_KEY に設定してください。",
                    model_id,
                )
            return _error(f"HAI 文字起こしに失敗しました{suffix}: {detail}", model_id)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        text = getattr(response, "text", None)
        if not isinstance(text, str) and isinstance(response, dict):
            text = response.get("text")
        if not isinstance(text, str):
            text = str(response)
        text = text.strip()
        # HAI は課金した秒数を usage.seconds で返す (1 秒未満切り上げ・最低 1 秒)。
        usage = getattr(response, "usage", None)
        seconds = getattr(usage, "seconds", None) if usage is not None else None
        if seconds is None and isinstance(usage, dict):
            seconds = usage.get("seconds")
        logger.info("Transcribed %s via HAI (%s, %d chars, billed %s s)",
                    audio.name, model_id, len(text), seconds if seconds is not None else "?")
        return _ok(text, model_id)


def register(ctx) -> None:
    """プラグインエントリポイント — HAI バックエンドを transcription レジストリに登録。"""
    ctx.register_transcription_provider(HaiTranscriptionProvider())

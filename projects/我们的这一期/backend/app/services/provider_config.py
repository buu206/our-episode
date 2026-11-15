"""Explicit provider selection. Configuration inspection never opens a client."""
from .director import DirectorError

STRINGS = {
    'VISION_PROVIDER': 'bailian', 'DIRECTOR_PROVIDER': 'bailian',
    'ASR_PROVIDER': 'bailian', 'TTS_PROVIDER': 'bailian',
    'DEEPSEEK_API_KEY': '', 'DEEPSEEK_BASE_URL': 'https://api.deepseek.com',
    'DEEPSEEK_MODEL': 'deepseek-flash', 'VOLC_API_KEY': '',
    'VOLC_ASR_RESOURCE': 'volc.seedasr.sauc.duration',
    'VOLC_TTS_RESOURCE': 'seed-tts-2.0',
    'MINIMAX_API_KEY': '', 'MINIMAX_TTS_MODEL': 'speech-2.8-hd', 'MINIMAX_ASR_MODEL': 'asr-1.0',
    'MINIMAX_TTS_VOICE': 'male-qn-daxuesheng', 'MINIMAX_TTS_SPEED': '1.15',
    'MINIMAX_TTS_EMOTION': 'happy', 'MINIMAX_TTS_PITCH': '0',
    'VOLC_TTS_SPEAKER': 'zh_female_vv_uranus_bigtts',
}
SWITCHES = tuple(f'{vendor}_{condition}' for vendor in ('DEEPSEEK', 'VOLC', 'MINIMAX')
                 for condition in ('ENABLED', 'RETENTION_VERIFIED', 'REGION_VERIFIED'))


def cloud_configuration(values):
    result = {key: values.get(key, default) for key, default in STRINGS.items()}
    for key in ('MODEL_API_KEY', 'MODEL_BASE_URL', 'MODEL_NAME'):
        result[key] = values.get(key, '')
    result['ASR_MODEL'] = values.get('ASR_MODEL', 'fun-asr-flash-2026-06-15')
    result['TTS_MODEL'] = values.get('TTS_MODEL', 'qwen3-tts-flash')
    for key in (*SWITCHES, 'CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED'):
        value = values.get(key, '')
        result[key] = value is True or (isinstance(value, str) and value.lower() == 'true')
    return result


def guard(config, vendor):
    if config.get('CLOUD_ENABLED') is not True or config.get(vendor + '_ENABLED') is not True:
        raise DirectorError('CLOUD_DISABLED', '所选模型服务尚未启用。')
    for condition, code, message in (
        ('RETENTION_VERIFIED', 'RETENTION_UNVERIFIED', '所选供应商留存和删除条件尚未核实。'),
        ('REGION_VERIFIED', 'REGION_UNVERIFIED', '所选供应商实际处理地区尚未核实。'),
    ):
        if config.get(vendor + '_' + condition) is not True:
            raise DirectorError(code, message)
    key = config.get(vendor + '_API_KEY')
    if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
        raise DirectorError('KEY_NOT_CONFIGURED', '所选供应商服务端密钥未配置。')


def adapter(config, kind, **kwargs):
    selected = config.get(kind.upper() + '_PROVIDER', 'bailian')
    if selected == 'bailian':
        from .director import CloudDirector
        from .vision import CloudVision
        from .speech import CloudSpeech
        return {'vision': CloudVision, 'director': CloudDirector,
                'asr': CloudSpeech, 'tts': CloudSpeech}[kind](config, **kwargs)
    if selected == 'deepseek' and kind in ('vision', 'director'):
        from .deepseek import DeepSeekVision, DeepSeekDirector
        return (DeepSeekVision if kind == 'vision' else DeepSeekDirector)(config, **kwargs)
    if selected == 'minimax' and kind == 'tts':
        from .minimax_speech import MiniMaxSpeech
        return MiniMaxSpeech(config, **kwargs)
    if selected == 'minimax' and kind == 'asr':
        from .minimax_asr import MiniMaxASR
        return MiniMaxASR(config, **kwargs)
    if selected == 'volc' and kind in ('asr', 'tts'):
        from .volc_speech import VolcSpeech
        return VolcSpeech(config, capability=kind, **kwargs)
    raise DirectorError('PROVIDER_NOT_ALLOWED', '模型供应商或能力配置不受支持；未自动切换服务。')

from pathlib import Path

# --- handler.py ---
handler = Path('vps-cloud/routers/handler.py')
t = handler.read_text(encoding='utf-8')

hf_block = '''try:
    from huggingface_hub import hf_hub_download as _hf_hub_download
except Exception:  # noqa: BLE001
    _hf_hub_download = None

try:
    from llama_cpp import Llama as _Llama
except Exception:  # noqa: BLE001
    _Llama = None

'''
spacy_anchor = '''try:
    import spacy as _spacy
except Exception:  # noqa: BLE001
    _spacy = None

'''
if '_hf_hub_download' not in t and spacy_anchor in t:
    t = t.replace(spacy_anchor, spacy_anchor + hf_block, 1)

const_anchor = '_DEVICE_SHARE_QWEN_MAX_TOKENS = int((os.environ.get("DISCORD_DEVICE_SHARE_QWEN_MAX_TOKENS", "72") or "72").strip() or "72")\n'
if '_DEVICE_SHARE_QWEN_RUNTIME_DEFAULT' not in t and const_anchor in t:
    t = t.replace(
        const_anchor,
        const_anchor +
        '_DEVICE_SHARE_QWEN_RUNTIME_DEFAULT = (os.environ.get("DISCORD_DEVICE_SHARE_QWEN_RUNTIME", "auto") or "auto").strip().lower()\n'
        '_DEVICE_SHARE_QWEN_REPO_DEFAULT = (os.environ.get("DISCORD_DEVICE_SHARE_QWEN_REPO_ID", "thirdeyeai/Qwen2.5-0.5B-Instruct-uncensored-GGUF") or "thirdeyeai/Qwen2.5-0.5B-Instruct-uncensored-GGUF").strip()\n'
        '_DEVICE_SHARE_QWEN_FILE_DEFAULT = (os.environ.get("DISCORD_DEVICE_SHARE_QWEN_FILENAME", "Qwen2.5-0.5B-Instruct-uncensored-q8_0.gguf") or "Qwen2.5-0.5B-Instruct-uncensored-q8_0.gguf").strip()\n'
        '_DEVICE_SHARE_QWEN_CTX_DEFAULT = int((os.environ.get("DISCORD_DEVICE_SHARE_QWEN_N_CTX", "512") or "512").strip() or "512")\n'
        '_DEVICE_SHARE_QWEN_GPU_LAYERS_DEFAULT = int((os.environ.get("DISCORD_DEVICE_SHARE_QWEN_N_GPU_LAYERS", "-1") or "-1").strip() or "-1")\n'
        '_LOCAL_QWEN_LLM: Any = None\n'
        '_LOCAL_QWEN_FAILED = False\n'
        '_LOCAL_QWEN_FINGERPRINT: Optional[str] = None\n'
    ,
        1,
    )

handler.write_text(t, encoding='utf-8')
print('handler updated')

# --- admin.py ---
admin = Path('vps-cloud/routers/admin.py')
a = admin.read_text(encoding='utf-8')

patch_anchor = '    discord_device_share_qwen_model: Optional[str] = None\n'
if 'discord_device_share_qwen_runtime' not in a and patch_anchor in a:
    a = a.replace(
        patch_anchor,
        patch_anchor +
        '    discord_device_share_qwen_runtime: Optional[str] = None\n'
        '    discord_device_share_qwen_repo_id: Optional[str] = None\n'
        '    discord_device_share_qwen_filename: Optional[str] = None\n'
        '    discord_device_share_qwen_n_ctx: Optional[int] = None\n'
        '    discord_device_share_qwen_n_gpu_layers: Optional[int] = None\n',
        1,
    )

get_anchor = '        "discord_device_share_qwen_model":      get_setting(db, "discord_device_share_qwen_model"),\n'
if '"discord_device_share_qwen_runtime":' not in a and get_anchor in a:
    a = a.replace(
        get_anchor,
        get_anchor +
        '        "discord_device_share_qwen_runtime":    get_setting(db, "discord_device_share_qwen_runtime"),\n'
        '        "discord_device_share_qwen_repo_id":    get_setting(db, "discord_device_share_qwen_repo_id"),\n'
        '        "discord_device_share_qwen_filename":   get_setting(db, "discord_device_share_qwen_filename"),\n'
        '        "discord_device_share_qwen_n_ctx":      get_setting(db, "discord_device_share_qwen_n_ctx"),\n'
        '        "discord_device_share_qwen_n_gpu_layers": get_setting(db, "discord_device_share_qwen_n_gpu_layers"),\n',
        1,
    )

str_fields_anchor = '        ("discord_device_share_qwen_model", body.discord_device_share_qwen_model),\n'
if '("discord_device_share_qwen_runtime", body.discord_device_share_qwen_runtime)' not in a and str_fields_anchor in a:
    a = a.replace(
        str_fields_anchor,
        str_fields_anchor +
        '        ("discord_device_share_qwen_runtime", body.discord_device_share_qwen_runtime),\n'
        '        ("discord_device_share_qwen_repo_id", body.discord_device_share_qwen_repo_id),\n'
        '        ("discord_device_share_qwen_filename", body.discord_device_share_qwen_filename),\n',
        1,
    )

if 'discord_device_share_qwen_n_ctx' in a and '("discord_device_share_qwen_n_ctx", body.discord_device_share_qwen_n_ctx)' not in a:
    bool_anchor = '    bool_fields = [\n'
    insert = '    int_fields = [\n        ("discord_device_share_qwen_n_ctx", body.discord_device_share_qwen_n_ctx),\n        ("discord_device_share_qwen_n_gpu_layers", body.discord_device_share_qwen_n_gpu_layers),\n    ]\n\n'
    if bool_anchor in a:
        a = a.replace(bool_anchor, insert + bool_anchor, 1)
        loop_anchor = '    for key, val in bool_fields:\n'
        if loop_anchor in a:
            a = a.replace(
                loop_anchor,
                '    for key, val in int_fields:\n'
                '        if val is not None:\n'
                '            set_setting(db, key, str(val))\n'
                '            updated.append(key)\n\n' + loop_anchor,
                1,
            )

admin.write_text(a, encoding='utf-8')
print('admin updated')

# --- requirements.txt ---
req = Path('vps-cloud/requirements.txt')
r = req.read_text(encoding='utf-8').splitlines()
for dep in ['huggingface_hub>=0.23.0', 'llama-cpp-python>=0.2.90']:
    if dep not in r:
        r.append(dep)
req.write_text('\n'.join(r) + '\n', encoding='utf-8')
print('requirements updated')

# --- compose.yaml ---
compose = Path('vps-cloud/compose.yaml')
c = compose.read_text(encoding='utf-8')
anchor = '      - DISCORD_DEVICE_SHARE_QWEN_MAX_TOKENS=${DISCORD_DEVICE_SHARE_QWEN_MAX_TOKENS:-72}\n'
if 'DISCORD_DEVICE_SHARE_QWEN_RUNTIME' not in c and anchor in c:
    c = c.replace(
        anchor,
        anchor +
        '      - DISCORD_DEVICE_SHARE_QWEN_RUNTIME=${DISCORD_DEVICE_SHARE_QWEN_RUNTIME:-auto}\n'
        '      - DISCORD_DEVICE_SHARE_QWEN_REPO_ID=${DISCORD_DEVICE_SHARE_QWEN_REPO_ID:-thirdeyeai/Qwen2.5-0.5B-Instruct-uncensored-GGUF}\n'
        '      - DISCORD_DEVICE_SHARE_QWEN_FILENAME=${DISCORD_DEVICE_SHARE_QWEN_FILENAME:-Qwen2.5-0.5B-Instruct-uncensored-q8_0.gguf}\n'
        '      - DISCORD_DEVICE_SHARE_QWEN_N_CTX=${DISCORD_DEVICE_SHARE_QWEN_N_CTX:-512}\n'
        '      - DISCORD_DEVICE_SHARE_QWEN_N_GPU_LAYERS=${DISCORD_DEVICE_SHARE_QWEN_N_GPU_LAYERS:--1}\n',
        1,
    )
compose.write_text(c, encoding='utf-8')
print('compose updated')

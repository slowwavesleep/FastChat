"""
The gradio demo server for chatting with a single model.
"""

import argparse
from collections import defaultdict
import datetime
import hashlib
import json
import os
import random
import time
import uuid
from typing import List, Dict

import gradio as gr
import requests

from fastchat.constants import (
    LOGDIR,
    WORKER_API_TIMEOUT,
    ErrorCode,
    MODERATION_MSG,
    CONVERSATION_LIMIT_MSG,
    RATE_LIMIT_MSG,
    SERVER_ERROR_MSG,
    INPUT_CHAR_LEN_LIMIT,
    CONVERSATION_TURN_LIMIT,
    SESSION_EXPIRATION_TIME,
    SURVEY_LINK,
)
from fastchat.model.model_adapter import (
    get_conversation_template,
)
from fastchat.model.model_registry import get_model_info, model_info
from fastchat.serve.api_provider import get_api_provider_stream_iter
from fastchat.serve.gradio_global_state import Context
from fastchat.serve.remote_logger import get_remote_logger
from fastchat.utils import (
    build_logger,
    get_window_url_params_js,
    get_window_url_params_with_tos_js,
    moderation_filter,
    parse_gradio_auth_creds,
    load_image,
)

logger = build_logger("gradio_web_server", "gradio_web_server.log")

headers = {"User-Agent": "FastChat Client"}

no_change_btn = gr.Button()
enable_btn = gr.Button(interactive=True, visible=True)
disable_btn = gr.Button(interactive=False)
invisible_btn = gr.Button(interactive=False, visible=False)
enable_text = gr.Textbox(
    interactive=True,
    visible=True,
    placeholder="👉 Kirjuta siia enda küsimus ja vajuta ENTER",
)
disable_text = gr.Textbox(
    interactive=False,
    visible=True,
    placeholder='Kliki "🎲 Uus vestlus" et uut vestlust alustada.',
)

controller_url = None
enable_moderation = False
use_remote_storage = False

acknowledgment_md = """
Kasutajad on kohustatud nõustuma järgmiste kasutustingimustega:

Antud platvorm on loodud eelistusandmete ja juhiste kogumiseks parema eestikeelse tehisaru loomise eesmärgil. Kuigi kasutajate platvormile sisestatud tekst läbib automaatse modereerimise, ei ole see kahjuks täiuslik, mistõttu võivad mudelid genereerida solvavat sisu. Antud platvormi ei tohi kasutada ebaseadusliku, kedagi kahjustava, vägivaldse, rassistliku või seksuaalse sisuga teksti loomiseks. 
Palun ära siseseta platvormile isiklikku informatsiooni. Platvorm kogub kasutajate vestlusandmeid ning jätab endale õiguse neid andmeid levitada Creative Commons Attribution (CC-BY) või sarnase litsentsi alusel. 
"""

# JSON file format of API-based models:
# {
#   "gpt-3.5-turbo": {
#     "model_name": "gpt-3.5-turbo",
#     "api_type": "openai",
#     "api_base": "https://api.openai.com/v1",
#     "api_key": "sk-******",
#     "anony_only": false
#   }
# }
#
#  - "api_type" can be one of the following: openai, anthropic, gemini, or mistral. For custom APIs, add a new type and implement it accordingly.
#  - "anony_only" indicates whether to display this model in anonymous mode only.

api_endpoint_info = {}


class State:
    def __init__(self, model_name, is_vision=False):
        self.conv = get_conversation_template(model_name)
        self.conv_id = uuid.uuid4().hex
        self.skip_next = False
        self.model_name = model_name
        self.oai_thread_id = None
        self.is_vision = is_vision
        self.ans_models = []
        self.router_outputs = []

        # NOTE(chris): This could be sort of a hack since it assumes the user only uploads one image. If they can upload multiple, we should store a list of image hashes.
        self.has_csam_image = False

        self.regen_support = True
        if "browsing" in model_name:
            self.regen_support = False
        self.init_system_prompt(self.conv, is_vision)

    def update_ans_models(self, ans: str) -> None:
        self.ans_models.append(ans)

    def update_router_outputs(self, outputs: Dict[str, float]) -> None:
        self.router_outputs.append(outputs)

    def init_system_prompt(self, conv, is_vision):
        system_prompt = conv.get_system_message(is_vision)
        if len(system_prompt) == 0:
            return
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        system_prompt = system_prompt.replace("{{currentDateTime}}", current_date)

        current_date_v2 = datetime.datetime.now().strftime("%d %b %Y")
        system_prompt = system_prompt.replace("{{currentDateTimev2}}", current_date_v2)

        current_date_v3 = datetime.datetime.now().strftime("%B %Y")
        system_prompt = system_prompt.replace("{{currentDateTimev3}}", current_date_v3)
        conv.set_system_message(system_prompt)

    def to_gradio_chatbot(self):
        return self.conv.to_gradio_chatbot()

    def dict(self):
        base = self.conv.dict()
        base.update(
            {
                "conv_id": self.conv_id,
                "model_name": self.model_name,
            }
        )

        if self.ans_models:
            base.update(
                {
                    "ans_models": self.ans_models,
                }
            )

        if self.router_outputs:
            base.update(
                {
                    "router_outputs": self.router_outputs,
                }
            )

        if self.is_vision:
            base.update({"has_csam_image": self.has_csam_image})
        return base


def set_global_vars(
    controller_url_,
    enable_moderation_,
    use_remote_storage_,
):
    global controller_url, enable_moderation, use_remote_storage
    controller_url = controller_url_
    enable_moderation = enable_moderation_
    use_remote_storage = use_remote_storage_


def get_conv_log_filename(is_vision=False, has_csam_image=False):
    t = datetime.datetime.now()
    conv_log_filename = f"{t.year}-{t.month:02d}-{t.day:02d}-conv.json"
    if is_vision and not has_csam_image:
        name = os.path.join(LOGDIR, f"vision-tmp-{conv_log_filename}")
    elif is_vision and has_csam_image:
        name = os.path.join(LOGDIR, f"vision-csam-{conv_log_filename}")
    else:
        name = os.path.join(LOGDIR, conv_log_filename)

    return name


def get_model_list(controller_url, register_api_endpoint_file, vision_arena):
    global api_endpoint_info

    # Add models from the controller
    if controller_url:
        ret = requests.post(controller_url + "/refresh_all_workers")
        assert ret.status_code == 200

        if vision_arena:
            ret = requests.post(controller_url + "/list_multimodal_models")
            models = ret.json()["models"]
        else:
            ret = requests.post(controller_url + "/list_language_models")
            models = ret.json()["models"]
    else:
        models = []

    # Add models from the API providers
    if register_api_endpoint_file:
        api_endpoint_info = json.load(open(register_api_endpoint_file))
        for mdl, mdl_dict in api_endpoint_info.items():
            mdl_vision = mdl_dict.get("vision-arena", False)
            mdl_text = mdl_dict.get("text-arena", True)
            if vision_arena and mdl_vision:
                models.append(mdl)
            if not vision_arena and mdl_text:
                models.append(mdl)

    # Remove anonymous models
    models = list(set(models))
    visible_models = models.copy()
    for mdl in models:
        if mdl not in api_endpoint_info:
            continue
        mdl_dict = api_endpoint_info[mdl]
        if mdl_dict["anony_only"]:
            visible_models.remove(mdl)

    # Sort models and add descriptions
    priority = {k: f"___{i:03d}" for i, k in enumerate(model_info)}
    models.sort(key=lambda x: priority.get(x, x))
    visible_models.sort(key=lambda x: priority.get(x, x))
    logger.info(f"All models: {models}")
    logger.info(f"Visible models: {visible_models}")
    return visible_models, models


def load_demo_single(context: Context, query_params):
    # default to text models
    models = context.text_models

    selected_model = models[0] if len(models) > 0 else ""
    if "model" in query_params:
        model = query_params["model"]
        if model in models:
            selected_model = model

    all_models = context.models

    dropdown_update = gr.Dropdown(
        choices=all_models, value=selected_model, visible=True
    )
    state = None
    return [state, dropdown_update]


def load_demo(url_params, request: gr.Request):
    global models

    ip = get_ip(request)
    logger.info(f"load_demo. ip: {ip}. params: {url_params}")

    if args.model_list_mode == "reload":
        models, all_models = get_model_list(
            controller_url, args.register_api_endpoint_file, vision_arena=False
        )

    return load_demo_single(models, url_params)


def vote_last_response(state, vote_type, model_selector, request: gr.Request):
    filename = get_conv_log_filename()
    if "llava" in model_selector:
        filename = filename.replace("2024", "vision-tmp-2024")

    with open(filename, "a") as fout:
        data = {
            "tstamp": round(time.time(), 4),
            "type": vote_type,
            "model": model_selector,
            "state": state.dict(),
            "ip": get_ip(request),
        }
        fout.write(json.dumps(data) + "\n")
    get_remote_logger().log(data)


def upvote_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"upvote. ip: {ip}")
    vote_last_response(state, "upvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def downvote_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"downvote. ip: {ip}")
    vote_last_response(state, "downvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def flag_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"flag. ip: {ip}")
    vote_last_response(state, "flag", model_selector, request)
    return ("",) + (disable_btn,) * 3


def regenerate(state, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"regenerate. ip: {ip}")
    if not state.regen_support:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "", None) + (no_change_btn,) * 5
    state.conv.update_last_message(None)
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 5


def clear_history(request: gr.Request):
    ip = get_ip(request)
    logger.info(f"clear_history. ip: {ip}")
    state = None
    return (state, [], "") + (disable_btn,) * 5


def get_ip(request: gr.Request):
    if "cf-connecting-ip" in request.headers:
        ip = request.headers["cf-connecting-ip"]
    elif "x-forwarded-for" in request.headers:
        ip = request.headers["x-forwarded-for"]
        if "," in ip:
            ip = ip.split(",")[0]
    else:
        ip = request.client.host
    return ip


def add_text(state, model_selector, text, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"add_text. ip: {ip}. len: {len(text)}")

    if state is None:
        state = State(model_selector)

    if len(text) <= 0:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "", None) + (no_change_btn,) * 5

    all_conv_text = state.conv.get_prompt()
    all_conv_text = all_conv_text[-2000:] + "\nuser: " + text
    flagged = moderation_filter(all_conv_text, [state.model_name])
    # flagged = moderation_filter(text, [state.model_name])
    if flagged:
        logger.info(f"violate moderation. ip: {ip}. text: {text}")
        # overwrite the original text
        text = MODERATION_MSG

    if (len(state.conv.messages) - state.conv.offset) // 2 >= CONVERSATION_TURN_LIMIT:
        logger.info(f"conversation turn limit. ip: {ip}. text: {text}")
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), CONVERSATION_LIMIT_MSG, None) + (
            no_change_btn,
        ) * 5

    text = text[:INPUT_CHAR_LEN_LIMIT]  # Hard cut-off
    state.conv.append_message(state.conv.roles[0], text)
    state.conv.append_message(state.conv.roles[1], None)
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 5


def model_worker_stream_iter(
    conv,
    model_name,
    worker_addr,
    prompt,
    temperature,
    repetition_penalty,
    top_p,
    max_new_tokens,
    images,
):
    # Make requests
    gen_params = {
        "model": model_name,
        "prompt": prompt,
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "max_new_tokens": max_new_tokens,
        "stop": conv.stop_str,
        "stop_token_ids": conv.stop_token_ids,
        "echo": False,
    }

    logger.info(f"==== request ====\n{gen_params}")

    if len(images) > 0:
        gen_params["images"] = images

    # Stream output
    response = requests.post(
        worker_addr + "/worker_generate_stream",
        headers=headers,
        json=gen_params,
        stream=True,
        timeout=WORKER_API_TIMEOUT,
    )
    for chunk in response.iter_lines(decode_unicode=False, delimiter=b"\0"):
        if chunk:
            data = json.loads(chunk.decode())
            yield data


def is_limit_reached(model_name, ip):
    monitor_url = "http://localhost:9090"
    try:
        ret = requests.get(
            f"{monitor_url}/is_limit_reached?model={model_name}&user_id={ip}", timeout=1
        )
        obj = ret.json()
        return obj
    except Exception as e:
        logger.info(f"monitor error: {e}")
        return None


def bot_response(
    state: State,
    temperature,
    top_p,
    max_new_tokens,
    request: gr.Request,
    apply_rate_limit=True,
    use_recommended_config=False,
):
    ip = get_ip(request)
    logger.info(f"bot_response. ip: {ip}")
    start_tstamp = time.time()
    temperature = float(temperature)
    top_p = float(top_p)
    max_new_tokens = int(max_new_tokens)

    if state.skip_next:
        # This generate call is skipped due to invalid inputs
        state.skip_next = False
        yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * 5
        return

    if apply_rate_limit:
        ret = is_limit_reached(state.model_name, ip)
        if ret is not None and ret["is_limit_reached"]:
            error_msg = RATE_LIMIT_MSG + "\n\n" + ret["reason"]
            logger.info(f"rate limit reached. ip: {ip}. error_msg: {ret['reason']}")
            state.conv.update_last_message(error_msg)
            yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * 5
            return

    conv, model_name = state.conv, state.model_name
    model_api_dict = (
        api_endpoint_info[model_name] if model_name in api_endpoint_info else None
    )
    images = conv.get_images()

    if model_api_dict is None:
        # Query worker address
        ret = requests.post(
            controller_url + "/get_worker_address", json={"model": model_name}
        )
        worker_addr = ret.json()["address"]
        logger.info(f"model_name: {model_name}, worker_addr: {worker_addr}")

        # No available worker
        if worker_addr == "":
            conv.update_last_message(SERVER_ERROR_MSG)
            yield (
                state,
                state.to_gradio_chatbot(),
                disable_btn,
                disable_btn,
                disable_btn,
                enable_btn,
                enable_btn,
            )
            return

        # Construct prompt.
        # We need to call it here, so it will not be affected by "▌".
        prompt = conv.get_prompt()
        # Set repetition_penalty
        if "t5" in model_name:
            repetition_penalty = 1.2
        else:
            repetition_penalty = 1.0

        stream_iter = model_worker_stream_iter(
            conv,
            model_name,
            worker_addr,
            prompt,
            temperature,
            repetition_penalty,
            top_p,
            max_new_tokens,
            images,
        )
    else:
        # Remove system prompt for API-based models unless specified
        custom_system_prompt = model_api_dict.get("custom_system_prompt", False)
        if not custom_system_prompt:
            conv.set_system_message("")

        if use_recommended_config:
            recommended_config = model_api_dict.get("recommended_config", None)
            if recommended_config is not None:
                temperature = recommended_config.get("temperature", temperature)
                top_p = recommended_config.get("top_p", top_p)
                max_new_tokens = recommended_config.get(
                    "max_new_tokens", max_new_tokens
                )

        stream_iter = get_api_provider_stream_iter(
            conv,
            model_name,
            model_api_dict,
            temperature,
            top_p,
            max_new_tokens,
            state,
        )

    html_code = ' <span class="cursor"></span> '

    # conv.update_last_message("▌")
    conv.update_last_message(html_code)
    yield (state, state.to_gradio_chatbot()) + (disable_btn,) * 5

    try:
        data = {"text": ""}
        for i, data in enumerate(stream_iter):
            # Change for P2L:
            if i == 0:
                if "ans_model" in data:
                    ans_model = data.get("ans_model")

                    state.update_ans_models(ans_model)

                if "router_outputs" in data:
                    router_outputs = data.get("router_outputs")

                    state.update_router_outputs(router_outputs)

            if data["error_code"] == 0:
                output = data["text"].strip()
                conv.update_last_message(output + "▌")
                # conv.update_last_message(output + html_code)
                yield (state, state.to_gradio_chatbot()) + (disable_btn,) * 5
            else:
                output = f"{SERVER_ERROR_MSG}\n\n" + data["text"] + f"\n\n(error_code: {data['error_code']})"
                conv.update_last_message(output)
                yield (state, state.to_gradio_chatbot()) + (
                    disable_btn,
                    disable_btn,
                    disable_btn,
                    enable_btn,
                    enable_btn,
                )
                return
        output = data["text"].strip()
        conv.update_last_message(output)
        yield (state, state.to_gradio_chatbot()) + (enable_btn,) * 5
    except requests.exceptions.RequestException as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_REQUEST_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (
            disable_btn,
            disable_btn,
            disable_btn,
            enable_btn,
            enable_btn,
        )
        return
    except Exception as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_STREAM_UNKNOWN_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (
            disable_btn,
            disable_btn,
            disable_btn,
            enable_btn,
            enable_btn,
        )
        return

    finish_tstamp = time.time()
    logger.info(f"{output}")

    conv.save_new_images(
        has_csam_images=state.has_csam_image, use_remote_storage=use_remote_storage
    )

    filename = get_conv_log_filename(
        is_vision=state.is_vision, has_csam_image=state.has_csam_image
    )

    with open(filename, "a") as fout:
        data = {
            "tstamp": round(finish_tstamp, 4),
            "type": "chat",
            "model": model_name,
            "gen_params": {
                "temperature": temperature,
                "top_p": top_p,
                "max_new_tokens": max_new_tokens,
            },
            "start": round(start_tstamp, 4),
            "finish": round(finish_tstamp, 4),
            "state": state.dict(),
            "ip": get_ip(request),
        }
        fout.write(json.dumps(data) + "\n")
    get_remote_logger().log(data)


block_css = """
body:not(.dark) #filters_row #filter_checkbox {
    background-color: white;
}

body.dark #filters_row #filter_checkbox {
    background-color: #0f0f0f;
}

#filters_row .form {
    border: none; 
    box-shadow: none; 
}

#filters_row .block {
    padding: 0; 
}

.wrap[data-testid="checkbox-group"] {
    display: flex;
}
.wrap[data-testid="checkbox-group"] label {
    border-top: 1px solid rgb(170, 170, 170);
    border-right: 1px solid rgb(170, 170, 170);
    border-bottom: 1px solid rgb(170, 170, 170);
    border-left: 1px solid rgb(170, 170, 170);
    flex: 1 1 230px;
    max-width: 250px;
}

.wrap[data-testid="checkbox-group"] label span {
    height: 23px;
}

body:not(.dark) .wrap[data-testid="checkbox-group"] label {
    background: white;
    color: var(--link-text-color);
}

body:not(.dark) .wrap[data-testid="checkbox-group"] label.selected {
    background: white;
    color: var(--link-text-color);
}

body:not(.dark) .wrap[data-testid="checkbox-group"] label:hover {
    background: white;
    color: var(--link-text-color);
}

body:not(.dark) .wrap[data-testid="checkbox-group"] input {
    background-color: rgb(220, 220, 220);
}


body:not(.dark) .wrap[data-testid="checkbox-group"] input:hover {
    background-color: rgb(220, 220, 220);
}


body .wrap[data-testid="checkbox-group"] input {
    background: rgb(220, 220, 220); 
}

body .wrap[data-testid="checkbox-group"] input:hover {
    background: rgb(220, 220, 220); 
}

body.dark .wrap[data-testid="checkbox-group"] label:hover {
    background: #171717; 
}

body.dark .wrap[data-testid="checkbox-group"] label.selected {
    background: #171717; 
}

body.dark .wrap[data-testid="checkbox-group"] label {
    background: #171717; 
}


.prose {
    font-size: 105% !important;
}

.tabs {
    margin-bottom: 164px;
    margin-top: -32px;
}

#input_row {
    gap: 0;
}

body:not(.dark) #input_box, #input_row {
    background-color: #ffffff70;
}

#input_box textarea {
    font-size: 16px;
}

body:not(.dark) #input_box textarea::placeholder {
    color: #444;
}

body:not(.dark) #input_box textarea:not([disabled]) {
    background-color: white;
}

body.dark #input_box textarea::placeholder {
    color: white;
}

body.dark #input_box textarea {
    background-color: #444;
    color: #ddd;
}

.chatbot {
    box-shadow: none;
}

body:not(.dark) .chatbot_0 {
    border-left: 1px solid #e5e5e5 !important;
}

body:not(.dark) .chatbot_1 {
    border-right: 1px solid #e5e5e5 !important;
}

.voting_button {
    border-top: 1px solid #AAA;
    border-right: 1px solid #AAA;
    border-bottom: 1px solid #AAA;
}

/*
body:not(.dark) .voting_button {
    background: white;
    color: gray;
}
*/

body.dark .voting-button {
    border-top: 1px solid #666;
    border-right: 1px solid #666;
    border-bottom: 1px solid #666;
}

/*
.voting_button:hover {
    background: #DDD;
}
*/

.voting_button:first-child {
    border-left: 1px solid #AAA;
    border-top-left-radius: 6px;
}

.voting_button:nth-child(2) {
    border-right: 1px solid #AAA;
    border-top-right-radius: 6px;
}

#selection_buttons_row {
    gap: 0;
}

.hidden {
    display: none;
}

.bold {
    font-weight: 600;
}

body #hero_text {
    background-color: #e0f0ff;
    text-align: center;
    padding: 32px 24px 56px;
}

body.dark #hero_text {
    background-color: #394e61;
}

#hero_text h1 {
    font-size: 34px;
    padding-bottom: 24px;
}

#hero_text ol {
    font-size: 18px;
}

#hero_text ol li {
    padding: 2px 0;
}

#models_accordion {
    border-bottom: none !important;
    border-radius: 6px;
}

body:not(.dark) #models_accordion {
    border: 1px solid #e5e5e5 !important;
}

.contributor_logos_top {
    padding: 4px;
    padding-left: 12px;
    float: right;
    display: inline-flex;
}

body.dark .contributor_logos_top,
body.dark .contributor_logos_bottom {
    padding-left: 12px;
    background: #888;
    border-radius: 6px;
}

.contributor_logos_bottom {
    display: none;
    padding: 8px 0;
}

.contributor_logo {
    height: 28px;
}

.contributor_logos_top .contributor_logo {
    height: 32px;
}

.contributor_logo:not(:first-child) {
    margin-left: 12px;
}

#fixed_footer {
    position: fixed;
    bottom: 0px;
    left: 0px;
    width: calc(100% - 256px);
    z-index: 25;
    margin: 0 128px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
}


@media screen and (max-width: 1100px) {
    #fixed_footer {
        width: 100%;
        margin: 0;
    }
}


.voting_button {
    min-width: 50%;
    max-width: 50%;
}

button[disabled] {
    opacity: 1;
}

@media screen and (min-width: 640px) {
    .control_button:first-child {
        margin-right: 12px;
    }
}

body:not(.dark) #turnstile-container {
    background: white;
}
body.dark #turnstile-container {
    background: var(--background-fill-secondary);
}

@media screen and (max-width: 640px) {

    #turnstile-container {
        position: fixed;
        bottom: 6px;
        left: 4px;
        z-index: 26;
        background: unset;
    }

    .contributor_logos_top {
        display: none;
    }

    .contributor_logos_bottom {
        margin-top: 24px;
        display: flex;
        justify-content: space-around;
    }

    .chatbot {
        height: 450px !important;
    }


    .message-row.bubble {
        margin: var(--spacing-xl) var(--spacing-xl) var(--spacing-md) !important;
    }

    #fixed_footer {
        width: 100%;
        margin: 0;
        border-radius: 0;
    }
    
    #send_button {
        min-width: unset;
        padding: 0 16px;
    }

    #input_box textarea {
        padding: 8px;
        height: 56px !important;
    }

    #fixed_footer {
        position: unset;
    }

    #input_row {
        position: fixed;
        bottom: 0;
        left: 0;
        width: 100%;
        z-index: 25;
    }

    #chat_tab {
        padding: 0;
    }

    #hero_container {
        border-radius: 0;
        margin-bottom: -32px;
    }

    #hero_text h2 {
        font-size: 20px;
    }

    #hero_text h1 {
        font-size: 30px;
    }

    #hero_text ol {
        font-size: 16px;
    }

    #hero_text {
        padding: 2px 24px 24px;
    }

    #selection_buttons_row {
        position: fixed;
        bottom: 84px;
        left: 0;
        width: 100%;
        z-index: 25;
    }

    .voting_button {
        min-width: 50%;
        max-width: 50%;
    }

    .voting_button.secondary {
        font-size: 15.5px;
        min-height: 46px;
    }

    /*
    .voting_button:nth-child(1) {
        order: 1;
    }
    .voting_button:nth-child(2) {
        order: 3;
    }
    .voting_button:nth-child(3) {
        order: 4;
    }
    .voting_button:nth-child(4) {
        order: 2;
    }
    */

    .tabs {
        margin-bottom: 172px;
    }
}



#arena_leaderboard_dataframe table {
    font-size: 105%;
}
#full_leaderboard_dataframe table {
    font-size: 105%;
}

.tab-nav button {
    font-size: 18px;
}

.chatbot h1 {
    font-size: 130%;
}
.chatbot h2 {
    font-size: 120%;
}
.chatbot h3 {
    font-size: 110%;
}

#chatbot .prose {
    font-size: 90% !important;
}

.sponsor-image-about img {
    margin: 0 20px;
    margin-top: 20px;
    height: 40px;
    max-height: 100%;
    width: auto;
    float: left;
}

.cursor {
    display: inline-block;
    width: 7px;
    height: 1em;
    background-color: black;
    vertical-align: middle;
    animation: blink 1s infinite;
}

.dark .cursor {
    display: inline-block;
    width: 7px;
    height: 1em;
    background-color: white;
    vertical-align: middle;
    animation: blink 1s infinite;
}

@keyframes blink {
    0%, 50% { opacity: 1; }
    50.1%, 100% { opacity: 0; }
}

.app {
  max-width: 100% !important;
  padding-left: 5% !important;
  padding-right: 5% !important;
}

a {
    color: #1976D2; /* Your current link color, a shade of blue */
    text-decoration: none; /* Removes underline from links */
}
a:hover {
    color: #63A4FF; /* This can be any color you choose for hover */
    text-decoration: underline; /* Adds underline on hover */
}

.block {
  overflow-y: hidden !important;
}

.visualizer {
    overflow: hidden;
    height: 60vw;
    border: 1px solid lightgrey; 
    border-radius: 10px;
}

@media screen and (max-width: 769px) {
    .visualizer {
        height: 180vw;
        overflow-y: scroll;
        width: 100%;
        overflow-x: hidden;
    }
}
"""


# block_css = """
# #notice_markdown .prose {
#     font-size: 110% !important;
# }
# #notice_markdown th {
#     display: none;
# }
# #notice_markdown td {
#     padding-top: 6px;
#     padding-bottom: 6px;
# }
# #arena_leaderboard_dataframe table {
#     font-size: 110%;
# }
# #full_leaderboard_dataframe table {
#     font-size: 110%;
# }
# #model_description_markdown {
#     font-size: 110% !important;
# }
# #leaderboard_markdown .prose {
#     font-size: 110% !important;
# }
# #leaderboard_markdown td {
#     padding-top: 6px;
#     padding-bottom: 6px;
# }
# #leaderboard_dataframe td {
#     line-height: 0.1em;
# }
# #about_markdown .prose {
#     font-size: 110% !important;
# }
# #ack_markdown .prose {
#     font-size: 110% !important;
# }
# #chatbot .prose {
#     font-size: 105% !important;
# }
# .sponsor-image-about img {
#     margin: 0 20px;
#     margin-top: 20px;
#     height: 40px;
#     max-height: 100%;
#     width: auto;
#     float: left;
# }

# body {
#     --body-text-size: 14px;
# }

# .chatbot h1, h2, h3 {
#     margin-top: 8px; /* Adjust the value as needed */
#     margin-bottom: 0px; /* Adjust the value as needed */
#     padding-bottom: 0px;
# }

# .chatbot h1 {
#     font-size: 130%;
# }
# .chatbot h2 {
#     font-size: 120%;
# }
# .chatbot h3 {
#     font-size: 110%;
# }
# .chatbot p:not(:first-child) {
#     margin-top: 8px;
# }

# .typing {
#     display: inline-block;
# }

# """


def get_model_description_md(models):
    model_description_md = """
| | | |
| ---- | ---- | ---- |
"""
    ct = 0
    visited = set()
    for i, name in enumerate(models):
        minfo = get_model_info(name)
        if minfo.simple_name in visited:
            continue
        visited.add(minfo.simple_name)
        one_model_md = f"[{minfo.simple_name}]({minfo.link}): {minfo.description}"

        if ct % 3 == 0:
            model_description_md += "|"
        model_description_md += f" {one_model_md} |"
        if ct % 3 == 2:
            model_description_md += "\n"
        ct += 1
    return model_description_md


def build_terms():
    gr.Markdown(acknowledgment_md, elem_id="terms_markdown")


def build_about():
    about_markdown = """
## Meist

**Tehisaru baromeeter** on Eesti teadlaste loodud platvorm, mille eesmärk on hinnata, kui hästi mõistavad ja kasutavad tänapäeva tehisarud eesti keelt. Veebilehel [baromeeter.ai](https://baromeeter.ai) saab võrrelda erinevate keelemudelite vastuseid ning aidata kaasa nende järjestamisele – ja seeläbi eesti keele ja meele hoidmisele tehisaru ajastul.

Platvormi arendavad koostöös Tartu Ülikool, Tallinna Tehnikaülikool, Tallinna Ülikool ja Eesti Keele Instituut. Tehisaru baromeeter tugineb avatud lähtekoodiga [ChatBotArena](https://lmarena.ai/) platvormile, mille töötasid välja California Ülikooli Berkeleys ja LMSYS teadlased. Algne platvorm on kohandatud emakeelseks, et paremini hinnata keelemudelite võimekust just eesti keeles.

Tehisaru baromeetril on kolm funktsiooni:

1. **Regulaarselt uuenev edetabel:** Kasutajate hinnangute põhjal kujuneb pidevalt uuenev järjestus, mis näitab, millised mudelid on eesti keeles kõige kvaliteetsemad. Edetabel peegeledab nii mudelite keeleoskust, faktiteadmisi, ohutust kui stiili ja iseloomu.
2. **Andmete kogumine keelemudelite arendamiseks:** Iga antud hinnang ja küsimus aitab teadlastel koguda väärtuslikku sisendit, mida saab kasutada keelemudelite peenhäälestamiseks parema eesti keele toetuse eesmärgil.
3. **TI-teadlikkuse tõstmine:** Baromeeter on hea võimalus katsetada erinevaid keelemudeleid, võrrelda vastuseid ning saada paremat aimu keelemudelite tugevustest ja nõrkustest just meie keelekeskkonnas.

Kutsume kõiki huvilisi osalema! Projekti esialgne eesmärk on koguda kokku 50 000 võrdlust (praegust häälte arvu näed edetabeli vahelehel). Iga klikiga aitad parandada eesti keele nähtavust tehisaru maailmas.

**Meeskond:**  
- Kairit Sirts (projektijuht), Tartu Ülikool
- Hele-Andra Kuulmets, Tartu Ülikool
- Aleksei Dorkin, Tartu Ülikool
- Krister Kruusmaa, Tallinna Ülikool

**Meediakajastused:**
- [https://cs.ut.ee/et/uudis/aita-valja-valida-koige-paremini-eesti-keelt-oskav-tehisaru](https://cs.ut.ee/et/uudis/aita-valja-valida-koige-paremini-eesti-keelt-oskav-tehisaru) *06.05.2025*
- [https://digi.geenius.ee/blogi/keel-ja-tehnoloogia/krister-kruusmaa-tehisarust-eestlase-tegemisel-saab-igauks-oma-panuse-anda/](https://digi.geenius.ee/blogi/keel-ja-tehnoloogia/krister-kruusmaa-tehisarust-eestlase-tegemisel-saab-igauks-oma-panuse-anda/) *09.05.2025*
- [https://novaator.err.ee/1609699473/keelemudelid-voivad-eesti-keeles-anda-toest-kaugele-jaavaid-vastuseid](https://novaator.err.ee/1609699473/keelemudelid-voivad-eesti-keeles-anda-toest-kaugele-jaavaid-vastuseid) *20.05.2025*
- [https://jupiter.err.ee/1609688162/terevisioon?t=1315]([https://jupiter.err.ee/1609688162/terevisioon?t=1315]) *20.05.2025*
- [https://r2.err.ee/1609701368/tlu-lektor-krister-kruusmaa-baromeeter-ai-annab-tehisintellektile-vajalikud-peenhaalestusandmed/er](https://r2.err.ee/1609701368/tlu-lektor-krister-kruusmaa-baromeeter-ai-annab-tehisintellektile-vajalikud-peenhaalestusandmed/er) *22.05.2025*
- [https://kultuur.err.ee/1609717077/keeleminutid-mida-naitab-tehisaru-baromeeter](https://kultuur.err.ee/1609717077/keeleminutid-mida-naitab-tehisaru-baromeeter) *09.06.2025*

**Kontakt:**  
[baromeeter@tartunlp.ai](mailto:baromeeter@tartunlp.ai)

---

Tehisaru baromeeter on loodud teadusprojekti [„Eesti keele toetus suurtes generatiivsetes vabavaralistes keelemudelites”](https://www.etis.ee/Portal/Projects/Display/a420f147-a693-4e0e-ad9f-0570862d6a9f) raames. Projekti rahastatakse riiklikust programmist „Eesti keeletehnoloogia 2018–2027"

"""
    gr.Markdown(about_markdown, elem_id="about_markdown")


def build_single_model_ui(models, add_promotion_links=False):
    promotion = (
        f"""
[Blog](https://blog.lmarena.ai/blog/2023/arena/) | [GitHub](https://github.com/lm-sys/FastChat) | [Paper](https://arxiv.org/abs/2403.04132) | [Dataset](https://github.com/lm-sys/FastChat/blob/main/docs/dataset_release.md) | [Twitter](https://twitter.com/lmsysorg) | [Discord](https://discord.gg/6GXcFg3TH8) | [Kaggle Competition](https://www.kaggle.com/competitions/lmsys-chatbot-arena)

{SURVEY_LINK}

## 👇 Choose any model to chat
"""
        if add_promotion_links
        else ""
    )

    notice_markdown = f"""
# 🏔️ Chatbot Arena (formerly LMSYS): Free AI Chat to Compare & Test Best AI Chatbots
{promotion}
"""

    state = gr.State()
    gr.Markdown(notice_markdown, elem_id="notice_markdown")

    with gr.Group(elem_id="share-region-named"):
        with gr.Row(elem_id="model_selector_row"):
            model_selector = gr.Dropdown(
                choices=models,
                value=models[0] if len(models) > 0 else "",
                interactive=True,
                show_label=False,
                container=False,
            )
        with gr.Row():
            with gr.Accordion(
                f"🔍 Expand to see the descriptions of {len(models)} models",
                open=False,
            ):
                model_description_md = get_model_description_md(models)
                gr.Markdown(model_description_md, elem_id="model_description_markdown")

        chatbot = gr.Chatbot(
            elem_id="chatbot",
            label="Scroll down and start chatting",
            height=650,
            show_copy_button=True,
            latex_delimiters=[
                {"left": "$", "right": "$", "display": False},
                {"left": "$$", "right": "$$", "display": True},
                {"left": r"\(", "right": r"\)", "display": False},
                {"left": r"\[", "right": r"\]", "display": True},
            ],
        )
    with gr.Row():
        textbox = gr.Textbox(
            show_label=False,
            placeholder="👉 Enter your prompt and press ENTER",
            elem_id="input_box",
        )
        send_btn = gr.Button(value="Send", variant="primary", scale=0)

    with gr.Row() as button_row:
        upvote_btn = gr.Button(value="👍  Upvote", interactive=False)
        downvote_btn = gr.Button(value="👎  Downvote", interactive=False)
        flag_btn = gr.Button(value="⚠️  Flag", interactive=False)
        regenerate_btn = gr.Button(value="🔄  Regenerate", interactive=False)
        clear_btn = gr.Button(value="🗑️  Clear history", interactive=False)

    with gr.Accordion("Parameters", open=False) as parameter_row:
        temperature = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=0.7,
            step=0.1,
            interactive=True,
            label="Temperature",
        )
        top_p = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=1.0,
            step=0.1,
            interactive=True,
            label="Top P",
        )
        max_output_tokens = gr.Slider(
            minimum=16,
            maximum=2048,
            value=1024,
            step=64,
            interactive=True,
            label="Max output tokens",
        )

    # if add_promotion_links:
    #     gr.Markdown(acknowledgment_md, elem_id="ack_markdown")

    # Register listeners
    btn_list = [upvote_btn, downvote_btn, flag_btn, regenerate_btn, clear_btn]
    upvote_btn.click(
        upvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    downvote_btn.click(
        downvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    flag_btn.click(
        flag_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    regenerate_btn.click(regenerate, state, [state, chatbot, textbox] + btn_list).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )
    clear_btn.click(clear_history, None, [state, chatbot, textbox] + btn_list)

    model_selector.change(clear_history, None, [state, chatbot, textbox] + btn_list)

    textbox.submit(
        add_text,
        [state, model_selector, textbox],
        [state, chatbot, textbox] + btn_list,
    ).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )
    send_btn.click(
        add_text,
        [state, model_selector, textbox],
        [state, chatbot, textbox] + btn_list,
    ).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )

    return [state, model_selector]


def build_demo(models):
    with gr.Blocks(
        title="Chatbot Arena (formerly LMSYS): Free AI Chat to Compare & Test Best AI Chatbots",
        theme=gr.themes.Default(),
        css=block_css,
    ) as demo:
        url_params = gr.JSON(visible=False)

        state, model_selector = build_single_model_ui(models)

        if args.model_list_mode not in ["once", "reload"]:
            raise ValueError(f"Unknown model list mode: {args.model_list_mode}")

        if args.show_terms_of_use:
            load_js = get_window_url_params_with_tos_js
        else:
            load_js = get_window_url_params_js

        demo.load(
            load_demo,
            [url_params],
            [
                state,
                model_selector,
            ],
            js=load_js,
        )

    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--share",
        action="store_true",
        help="Whether to generate a public, shareable link",
    )
    parser.add_argument(
        "--controller-url",
        type=str,
        default="http://localhost:21001",
        help="The address of the controller",
    )
    parser.add_argument(
        "--concurrency-count",
        type=int,
        default=10,
        help="The concurrency count of the gradio queue",
    )
    parser.add_argument(
        "--model-list-mode",
        type=str,
        default="once",
        choices=["once", "reload"],
        help="Whether to load the model list once or reload the model list every time",
    )
    parser.add_argument(
        "--moderate",
        action="store_true",
        help="Enable content moderation to block unsafe inputs",
    )
    parser.add_argument(
        "--show-terms-of-use",
        action="store_true",
        help="Shows term of use before loading the demo",
    )
    parser.add_argument(
        "--register-api-endpoint-file",
        type=str,
        help="Register API-based model endpoints from a JSON file",
    )
    parser.add_argument(
        "--gradio-auth-path",
        type=str,
        help='Set the gradio authentication file path. The file should contain one or more user:password pairs in this format: "u1:p1,u2:p2,u3:p3"',
    )
    parser.add_argument(
        "--gradio-root-path",
        type=str,
        help="Sets the gradio root path, eg /abc/def. Useful when running behind a reverse-proxy or at a custom URL path prefix",
    )
    parser.add_argument(
        "--use-remote-storage",
        action="store_true",
        default=False,
        help="Uploads image files to google cloud storage if set to true",
    )
    args = parser.parse_args()
    logger.info(f"args: {args}")

    # Set global variables
    set_global_vars(args.controller_url, args.moderate, args.use_remote_storage)
    models, all_models = get_model_list(
        args.controller_url, args.register_api_endpoint_file, vision_arena=False
    )

    # Set authorization credentials
    auth = None
    if args.gradio_auth_path is not None:
        auth = parse_gradio_auth_creds(args.gradio_auth_path)

    # Launch the demo
    demo = build_demo(models)
    demo.queue(
        default_concurrency_limit=args.concurrency_count,
        status_update_rate=10,
        api_open=False,
    ).launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        max_threads=200,
        auth=auth,
        root_path=args.gradio_root_path,
    )

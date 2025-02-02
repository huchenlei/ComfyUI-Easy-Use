import json
import os
import torch
import comfy.model_patcher
import comfy.model_management
import comfy.model_detection as model_detection
import comfy.supported_models
from comfy.clip_vision import ClipVisionModel, Output
from comfy.utils import load_torch_file
from .chatglm.modeling_chatglm import ChatGLMModel, ChatGLMConfig
from .chatglm.tokenization_chatglm import ChatGLMTokenizer

class Kolors(comfy.supported_models.SDXL):
    unet_config = {
        "model_channels": 320,
        "use_linear_in_transformer": True,
        "transformer_depth": [0, 0, 2, 2, 10, 10],
        "context_dim": 2048,
        "adm_in_channels": 5632,
        "use_temporal_attention": False,
    }

if Kolors not in comfy.supported_models.models:
    comfy.supported_models.models += [Kolors]

class applyKolorsUnet:
    def __enter__(self):
        self.original_unet_config_from_diffusers_unet = model_detection.unet_config_from_diffusers_unet
        model_detection.unet_config_from_diffusers_unet = kolors_unet_config_from_diffusers_unet

    def __exit__(self, type, value, traceback):
        model_detection.unet_config_from_diffusers_unet = self.original_unet_config_from_diffusers_unet

def kolors_unet_config_from_diffusers_unet(state_dict, dtype=None):
    match = {}
    transformer_depth = []

    attn_res = 1
    count_blocks = model_detection.count_blocks
    down_blocks = count_blocks(state_dict, "down_blocks.{}")
    for i in range(down_blocks):
        attn_blocks = count_blocks(
            state_dict, "down_blocks.{}.attentions.".format(i) + '{}')
        res_blocks = count_blocks(
            state_dict, "down_blocks.{}.resnets.".format(i) + '{}')
        for ab in range(attn_blocks):
            transformer_count = count_blocks(
                state_dict, "down_blocks.{}.attentions.{}.transformer_blocks.".format(i, ab) + '{}')
            transformer_depth.append(transformer_count)
            if transformer_count > 0:
                match["context_dim"] = state_dict["down_blocks.{}.attentions.{}.transformer_blocks.0.attn2.to_k.weight".format(
                    i, ab)].shape[1]

        attn_res *= 2
        if attn_blocks == 0:
            for i in range(res_blocks):
                transformer_depth.append(0)

    match["transformer_depth"] = transformer_depth

    match["model_channels"] = state_dict["conv_in.weight"].shape[0]
    match["in_channels"] = state_dict["conv_in.weight"].shape[1]
    match["adm_in_channels"] = None
    if "class_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["class_embedding.linear_1.weight"].shape[1]
    elif "add_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["add_embedding.linear_1.weight"].shape[1]

    Kolors = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
              'num_classes': 'sequential', 'adm_in_channels': 5632, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
              'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 2, 2, 10, 10], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 10,
              'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 2, 2, 2, 10, 10, 10],
              'use_temporal_attention': False, 'use_temporal_resblock': False}

    supported_models = [Kolors]

    for unet_config in supported_models:
        matches = True
        for k in match:
            if match[k] != unet_config[k]:
                # print("key {} does not match".format(k), match[k], "||", unet_config[k])
                matches = False
                break
        if matches:
            return model_detection.convert_config(unet_config)
    return None

# chatglm3 model
class chatGLM3Model(torch.nn.Module):
    def __init__(self, textmodel_json_config=None, device='cpu', offload_device='cpu', model_path=None):
        super().__init__()
        if model_path is None:
            raise ValueError("model_path is required")
        self.device = device
        if textmodel_json_config is None:
            textmodel_json_config = os.path.join(
                os.path.dirname(os.path.realpath(__file__)),
                "chatglm",
                "config_chatglm.json"
            )
        with open(textmodel_json_config, 'r') as file:
            config = json.load(file)
        textmodel_json_config = ChatGLMConfig(**config)
        is_accelerate_available = False
        try:
            from accelerate import init_empty_weights
            from accelerate.utils import set_module_tensor_to_device
            is_accelerate_available = True
        except:
            pass

        from contextlib import nullcontext
        with (init_empty_weights() if is_accelerate_available else nullcontext()):
            self.text_encoder = ChatGLMModel(textmodel_json_config)
            if '4bit' in model_path:
                self.text_encoder.quantize(4)
            elif '8bit' in model_path:
                self.text_encoder.quantize(8)

        sd = load_torch_file(model_path)
        if is_accelerate_available:
            for key in sd:
                set_module_tensor_to_device(self.text_encoder, key, device=offload_device, value=sd[key])
        else:
            self.text_encoder.load_state_dict()

def load_chatglm3(model_path=None):
    if model_path is None:
        return

    load_device = comfy.model_management.text_encoder_device()
    offload_device = comfy.model_management.text_encoder_offload_device()

    glm3model = chatGLM3Model(
        device=load_device,
        offload_device=offload_device,
        model_path=model_path
    )
    tokenizer_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'chatglm', "tokenizer")
    tokenizer = ChatGLMTokenizer.from_pretrained(tokenizer_path)
    text_encoder = glm3model.text_encoder
    return {"text_encoder":text_encoder, "tokenizer":tokenizer}


# clipvision model
def clip_preprocess(image, size=336):
    mean = torch.tensor([ 0.48145466,0.4578275,0.40821073], device=image.device, dtype=image.dtype)
    std = torch.tensor([0.26862954,0.26130258,0.27577711], device=image.device, dtype=image.dtype)
    image = image.movedim(-1, 1)
    if not (image.shape[2] == size and image.shape[3] == size):
        scale = (size / min(image.shape[2], image.shape[3]))
        image = torch.nn.functional.interpolate(image, size=(round(scale * image.shape[2]), round(scale * image.shape[3])), mode="bicubic", antialias=True)
        h = (image.shape[2] - size)//2
        w = (image.shape[3] - size)//2
        image = image[:,:,h:h+size,w:w+size]
    image = torch.clip((255. * image), 0, 255).round() / 255.0
    return (image - mean.view([3,1,1])) / std.view([3,1,1])

class kolorsClipVisionModel(ClipVisionModel):
    def __init__(self, json_config):
        super().__init__(json_config)

    def encode_image(self, image):
        comfy.model_management.load_model_gpu(self.patcher)
        pixel_values = clip_preprocess(image.to(self.load_device), 336).float()
        out = self.model(pixel_values=pixel_values, intermediate_output=-2)

        outputs = Output()
        outputs["last_hidden_state"] = out[0].to(comfy.model_management.intermediate_device())
        outputs["image_embeds"] = out[2].to(comfy.model_management.intermediate_device())
        outputs["penultimate_hidden_states"] = out[1].to(comfy.model_management.intermediate_device())
        return outputs

def load_kolors_clip_vision(path):
    sd = load_torch_file(path)
    if "vision_model.encoder.layers.22.layer_norm1.weight" in sd:
        json_config = os.path.join(os.path.dirname(os.path.realpath(__file__)), "clip_vision_config_vitl_336.json")
    else:
        raise Exception("Unsupported clip vision model")
    clip = kolorsClipVisionModel(json_config)
    m, u = clip.load_sd(sd)
    if len(m) > 0:
        print("missing clip vision: {}".format(m))
    u = set(u)
    keys = list(sd.keys())
    for k in keys:
        if k not in u:
            t = sd.pop(k)
            del t
    return clip
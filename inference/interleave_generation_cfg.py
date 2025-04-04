import torch
import os
import sys
import random
import numpy as np

# set random seed
def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root_path)
from models.processing_orthus import OrthusProcessor
from models.modeling_orthus_for_inteleave_cfg import OrthusForConditionalGeneration
import torch.nn.functional as F
import json
from PIL import Image

ckpt_path = "SJTU-Deng-Lab/Orthus-7B-base"
processor = OrthusProcessor.from_pretrained(ckpt_path)
model = OrthusForConditionalGeneration.from_pretrained(
    ckpt_path,
    device_map="auto",
    torch_dtype=torch.bfloat16,
    attn_implementation='eager',
)

exp_dir = os.path.join(root_path, "results/interleave_generation_cfg")
os.makedirs(exp_dir, exist_ok=True)

set_seed(50)

prompt="Generate an overview of various popular animals. Include pictures and detailed descriptions for each animal."


interleave_inputs_con = processor([prompt], return_tensors="pt")
interleave_input_ids_con = interleave_inputs_con['input_ids'].to(model.device)

prompt_uncon="generate images"
interleave_inputs_uncon = processor([prompt_uncon], return_tensors="pt")
interleave_input_ids_uncon = interleave_inputs_uncon['input_ids'].to(model.device)



kwargs_con = {
    "input_ids": interleave_input_ids_con,
    "cfg_scale": 3.0,
    "interleave_output_format": True,
    "max_new_tokens": 4096,
    "do_sample": True,
    "attention_mask": interleave_inputs_con['attention_mask'].to(model.device),
    "use_cache": True,
}
kwargs_uncon = {
    "input_ids": interleave_input_ids_uncon,
    "cfg_scale": 3.0,
    "attention_mask": interleave_inputs_uncon['attention_mask'].to(model.device),
    "use_cache": True,
}


outputs = model.generate(
    multimodal_generation_mode_list=["interleaved-text-image","image-only"],
    kwargs_list=[kwargs_con, kwargs_uncon],
)

text_tokens = []
all_image_embeds_wo_quant = []

with torch.no_grad():
    for output in outputs:
        if len(output.shape) == 1:
            if torch.sum(output == 8196) == 0 and torch.sum(output == 8197) == 0:
                text_tokens.append(output)
        else:
            all_image_embeds_wo_quant.append(output)

    # decode image one by one
    num_images = len(all_image_embeds_wo_quant) // 1024
    for id in range(num_images):
        image_embeds_wo_quant = torch.cat(all_image_embeds_wo_quant[id * 1024:(id + 1) * 1024], dim=0).to(model.device)

        emb_dim = model.model.vqmodel.quantize.embedding.weight.shape[-1]
        image_embeds_wo_quant = image_embeds_wo_quant.view((1, *model.model.vqmodel.quantize.quant_state_dims, emb_dim))
        image_embeds_wo_quant = image_embeds_wo_quant.permute(0, 3, 1, 2).contiguous()

        hidden_states = model.model.vqmodel.post_quant_conv(image_embeds_wo_quant.to(model.model.vqmodel.post_quant_conv.weight.dtype))
        pixel_values_wo_quant = model.model.vqmodel.decoder(hidden_states)
        images_wo_quant = processor.postprocess_pixel_values(pixel_values_wo_quant)

        from torchvision.transforms.functional import to_pil_image
        images_wo_quant = [to_pil_image(img.detach().cpu()) for img in images_wo_quant]
        images_wo_quant[0].save(os.path.join(exp_dir, f'./{prompt}_img{id+1}_cfg3.jpg'))

    # decode generated text
    text_tokens = torch.cat(text_tokens, dim=0).to(model.device)
    text = processor.batch_decode(text_tokens.unsqueeze(0), skip_special_tokens=True)[0]

    with open(os.path.join(exp_dir, f'./{prompt}_text.txt'), "w", encoding="utf-8") as file:
        file.write(prompt + '\n' + text)

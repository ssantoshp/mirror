from fastapi import FastAPI, File, UploadFile
from PIL import Image
from typing import List, Optional, Union
import io, uvicorn, gc
import base64
from fastapi.responses import StreamingResponse
import torch
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel

# CODE FROM HUGGING FACE CLOTH SEGMENTATION
from network import U2NET

import os
#from PIL import Image
import cv2
import gdown
import argparse
import numpy as np

import torch
import torch.nn.functional as F
import torchvision.transforms as transforms

from collections import OrderedDict
from options import opt

def dilate_image_pil(img, diper=0.03):
    ocv_img = np.array(img)
    if len(ocv_img.shape) == 3:
        ocv_img = ocv_img[:, :, 0]
    disize = int(diper * min(ocv_img.shape[:2]))
    kernel = np.ones((disize, disize), np.uint8)
    diimg = cv2.dilate(ocv_img, kernel, iterations=1)
    return Image.fromarray(diimg)

def load_checkpoint(model, checkpoint_path):
    if not os.path.exists(checkpoint_path):
        print("----No checkpoints at given path----")
        return
    model_state_dict = torch.load(checkpoint_path, map_location=torch.device("cpu"))
    new_state_dict = OrderedDict()
    for k, v in model_state_dict.items():
        name = k[7:]  # remove `module.`
        new_state_dict[name] = v

    model.load_state_dict(new_state_dict)
    print("----checkpoints loaded from path: {}----".format(checkpoint_path))
    return model


def get_palette(num_cls):
    """ Returns the color map for visualizing the segmentation mask.
    Args:
        num_cls: Number of classes
    Returns:
        The color map
    """
    n = num_cls
    palette = [0] * (n * 3)
    for j in range(0, n):
        lab = j
        palette[j * 3 + 0] = 0
        palette[j * 3 + 1] = 0
        palette[j * 3 + 2] = 0
        i = 0
        while lab:
            palette[j * 3 + 0] |= (((lab >> 0) & 1) << (7 - i))
            palette[j * 3 + 1] |= (((lab >> 1) & 1) << (7 - i))
            palette[j * 3 + 2] |= (((lab >> 2) & 1) << (7 - i))
            i += 1
            lab >>= 3
    return palette


class Normalize_image(object):
    """Normalize given tensor into given mean and standard dev

    Args:
        mean (float): Desired mean to substract from tensors
        std (float): Desired std to divide from tensors
    """

    def __init__(self, mean, std):
        assert isinstance(mean, (float))
        if isinstance(mean, float):
            self.mean = mean

        if isinstance(std, float):
            self.std = std

        self.normalize_1 = transforms.Normalize(self.mean, self.std)
        self.normalize_3 = transforms.Normalize([self.mean] * 3, [self.std] * 3)
        self.normalize_18 = transforms.Normalize([self.mean] * 18, [self.std] * 18)

    def __call__(self, image_tensor):
        if image_tensor.shape[0] == 1:
            return self.normalize_1(image_tensor)

        elif image_tensor.shape[0] == 3:
            return self.normalize_3(image_tensor)

        elif image_tensor.shape[0] == 18:
            return self.normalize_18(image_tensor)

        else:
            assert "Please set proper channels! Normlization implemented only for 1, 3 and 18"


def apply_transform(img):
    transforms_list = []
    transforms_list += [transforms.ToTensor()]
    transforms_list += [Normalize_image(0.5, 0.5)]
    transform_rgb = transforms.Compose(transforms_list)
    return transform_rgb(img)


# RETURNS BASE64 STRING OF THE UPPER BODY MASK
def generate_mask(input_image, net, palette, device = 'cpu'):

    #img = Image.open(input_image).convert('RGB')
    img = input_image
    img_size = img.size
    img = img.resize((768, 768), Image.BICUBIC)
    image_tensor = apply_transform(img)
    image_tensor = torch.unsqueeze(image_tensor, 0)

    alpha_out_dir = os.path.join(opt.output,'alpha')
    cloth_seg_out_dir = os.path.join(opt.output,'cloth_seg')

    os.makedirs(alpha_out_dir, exist_ok=True)
    os.makedirs(cloth_seg_out_dir, exist_ok=True)

    with torch.no_grad():
        output_tensor = net(image_tensor.to(device))
        output_tensor = F.log_softmax(output_tensor[0], dim=1)
        output_tensor = torch.max(output_tensor, dim=1, keepdim=True)[1]
        output_tensor = torch.squeeze(output_tensor, dim=0)
        output_arr = output_tensor.cpu().numpy()

    classes_to_save = []

    # Check which classes are present in the image
    cls = 1 # UPPER BODY CLASS
    # for cls in range(1, 4):  # Exclude background class (0)
    if np.any(output_arr == cls):
        classes_to_save.append(cls)

    # Save alpha masks
    # for cls in classes_to_save:
    alpha_mask = (output_arr == cls).astype(np.uint8) * 255
    alpha_mask = alpha_mask[0]  # Selecting the first channel to make it 2D
    alpha_mask_img = Image.fromarray(alpha_mask, mode='L')
    alpha_mask_img = alpha_mask_img.resize(img_size, Image.BICUBIC)

    #dilate
    alpha_mask_img = dilate_image_pil(alpha_mask_img, diper=0.02)

    alpha_mask_img.save(os.path.join(alpha_out_dir, f'{cls}.png'))
    
    buffered = io.BytesIO()
    alpha_mask_img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

    # Save final cloth segmentations
    # cloth_seg = Image.fromarray(output_arr[0].astype(np.uint8), mode='P')
    # cloth_seg.putpalette(palette)
    # cloth_seg = cloth_seg.resize(img_size, Image.BICUBIC)
    # cloth_seg.save(os.path.join(cloth_seg_out_dir, 'final_seg.png'))
    # return cloth_seg



def check_or_download_model(file_path):
    if not os.path.exists(file_path):
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        url = "https://drive.google.com/uc?id=11xTBALOeUkyuaK3l60CpkYHLTmv7k3dY"
        gdown.download(url, file_path, quiet=False)
        print("Model downloaded successfully.")
    else:
        print("Model already exists.")


def load_seg_model(checkpoint_path, device='cpu'):
    net = U2NET(in_ch=3, out_ch=4)
    check_or_download_model(checkpoint_path)
    net = load_checkpoint(net, checkpoint_path)
    net = net.to(device)
    net = net.eval()

    return net

# TAKES IN IMG AS A BASE64
# THIS IS THE MAIN FUNCTION
def get_upper_mask(img64, cuda=True, checkpoint_path="model/cloth_segm.pth"):

    device = 'cuda:0' if cuda else 'cpu'

    # Create an instance of your model
    model = load_seg_model(checkpoint_path, device=device)

    palette = get_palette(4)

    # img = Image.open(args.image).convert('RGB')
    # decode Base64
    img = Image.open(io.BytesIO(base64.b64decode(img64))).convert('RGB')

    upper_mask = generate_mask(img, net=model, palette=palette, device=device)
    return upper_mask
# END CODE TO RUN CLOTH SEGMENTATION MODEL

class segReq(BaseModel):
    img: str

app = FastAPI()
app.POOL: ThreadPoolExecutor = None

@app.on_event("startup")
def startup_event():
    app.POOL = ThreadPoolExecutor(max_workers=1)
@app.on_event("shutdown")
def shutdown_event():
    app.POOL.shutdown(wait=False)


@app.post("/seg")
def seg(body: segReq):
    return {"img": get_upper_mask(body.img)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=345)
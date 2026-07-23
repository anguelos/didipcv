import re
from PIL import Image
from typing import Tuple
from io import BytesIO
import requests
from .image_pager import create_pagers
#import base64
#from flask import Flask, jsonify, render_template, send_file, request


global_max_width = 3000
global_max_height = 3000


def _iiif_region(image: Image.Image, region:str, tecnical_properties:dict) -> Tuple[Image.Image, str]:
    if region == "full":
        pass
    elif region == "square":
        if image.size[0] > image.size[1]:
            image = image.crop((image.size[0]//2 - image.size[1]//2, 0, image.size[0]//2 + image.size[1]//2, image.size[1]))
        else:
            image = image.crop((0, image.size[1]//2 - image.size[0]//2, image.size[0], image.size[1]//2 + image.size[0]//2))
    elif len(re.findall('^pct:[0-9]+(\.[0-9]*)?,[0-9]+(\.[0-9]*)?,[0-9]+(\.[0-9]*)?,[0-9]+(\.[0-9]*)?$', region)) == 1: # assuming x,y,w,h:
        l, t, r, b = [float(d) for d in region[4:].split(",")]
        image = image.crop((int(image.size[0]*l/100), int(image.size[1]*t/100), int(image.size[0]*r/100), int(image.size[1]*b/100)))
    elif len(re.findall('^[0-9]+,[0-9]+,[0-9]+,[0-9]+$', region))==1: # assuming x,y,w,h
        l, t, r, b = [int(d) for d in region.split(",")]
        image = image.crop((l, t, r, b))
    else:
        return None, f"Region '{region}' not supported"
    return image, ""


def _iiif_size(image: Image.Image, size: str, tecnical_properties: dict) -> Tuple[Image.Image, str]:
    aspect_ratio = image.size[0]/image.size[1]
    max_width = tecnical_properties["maxWidth"]
    max_height = tecnical_properties["maxHeight"]

    def _scale_to_fit_size(w, h):
        if w <= max_width and h <= max_height:
            return int(w), int(h)
        if w > max_width and h <= max_height:
            return int(max_width), int(max_width/aspect_ratio)
        if w <= max_width and h > max_height:
            return int(max_height * aspect_ratio), int(max_height)
        if w > max_width and h > max_height:
            if w/h > max_width/max_height:
                return int(max_width), int(max_width/aspect_ratio)
            else:
                return int(max_height*aspect_ratio), int(max_height)
        raise Exception("Should not be here")

    if size == "max":
        new_width, new_height = _scale_to_fit_size(image.size[0], image.size[1])
        if new_width != image.size[0] or new_height != image.size[1]:
            image = image.resize((new_width, new_height))
    elif size == "^max":
        new_width = min((max_height * aspect_ratio, max_width))
        new_height = max((max_width / aspect_ratio, max_height))
        new_width, new_height = _scale_to_fit_size(new_width, new_height)
        image = image.resize((new_width, new_height))
    elif size.endswith(","):  # w, ^w,
        if size.startswith("^"):
            new_width = int(size[1:-1])
            new_height = int(new_width * aspect_ratio)
            new_width, new_height = _scale_to_fit_size(new_width, new_height)
            image = image.resize((new_width, new_height))
        else:  # w,
            if int(size[:-1]) < image.size[0]:
                new_width = int(size[:-1])
                new_height = int(new_width * aspect_ratio)
                image = image.resize((new_width, new_height))
            else:  # w, but w is bigger than the image
                return None, f"Size '{size}' not supported"
    elif size.startswith(","):  # ,h, ^,h
        if size.startswith("^"):
            new_height = int(size[1:])
            new_width = int(new_height / aspect_ratio)
            new_width, new_height = _scale_to_fit_size(new_width, new_height)
            image = image.resize((new_width, new_height))
        else:  # ,h
            if int(size[1:]) < image.size[1]:
                new_height = int(size[1:])
                new_width = int(new_height / aspect_ratio)
                image = image.resize((new_width, new_height))
            else:  # ,h, but h is bigger than the image
                return None, f"Size '{size}' not supported"
    elif len(re.findall('^\^?pct:[0-9]+(.[0-9]+)?$', size)) == 1:  # ^pct:50, pct:50
        if size.startswith("^"):  # ^pct:50
            new_width = int(image.size[0] * float(size[5:])/100)
            new_height = int(new_width * aspect_ratio)
            new_width, new_height = _scale_to_fit_size(new_width, new_height)
            image = image.resize((new_width, new_height))
        else: # pct:50
            if int(size[4:]) > 100:
                return None, f"Size '{size}' not supported"
            new_width = int(image.size[0] * float(size[4:])/100)
            new_height = int(new_width * aspect_ratio)
            new_width, new_height = _scale_to_fit_size(new_width, new_height)
            image = image.resize((new_width, new_height))
    elif len(re.findall('^\^?[0-9]+,[0-9]+$', size)) == 1: # ^w,h or w,h or !w,h or ^!w,h
        if size.startswith("^!"): # ^!w,h
            new_width, new_height = [int(d) for d in size[2:].split(",")]
            widths = [new_width, new_height * aspect_ratio, max_width]
            heights = [new_width / aspect_ratio, new_height, max_height]
            raise NotImplementedError("Not implemented yet")
        elif size.startswith("!"): # ^!w,h
            new_width, new_height = [int(d) for d in size[2:].split(",")]
            widths = [new_width, new_height * aspect_ratio, max_width]
            heights = [new_height, new_width / aspect_ratio, max_height]
            surfaces = [widths[i]*heights[i] for i in range(3)]
            raise NotImplementedError("Not implemented yet")
            if surfaces[0] <= surfaces[1] and surfaces[0]<=surfaces[2]:
                image = image.resize((widths[0], heights[0]))
            elif surfaces[1] <= surfaces[0] and surfaces[1]<=surfaces[2]:
                image = image.resize((widths[1], heights[1]))
            else:
                return None, f"Size '{size}' not supported"

        elif size.startswith("^"): # ^w,h
            new_width, new_height = _scale_to_fit_size(int(size[1:].split(",")[0]), int(size[1:].split(",")[1]))
            image = image.resize((new_width, new_height))
        elif size[0] in "0123456789": # w,h
            if new_width > image.size[0] and new_height > image.size[1]:
                new_width, new_height = _scale_to_fit_size(int(size.split(",")[0]), int(size.split(",")[1]))
                image = image.resize((new_width, new_height))
            else:
                return None, f"Size '{size}' not supported"
    else:
        return None, f"Size '{size}' not supported"
    return image, ""


def _iiif_rotation(image: Image.Image, rotation:str, tecnical_properties:dict) -> Tuple[Image.Image, str]:
    if rotation.startswith("!"):
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
        rotation = rotation[1:]
    rotation = int(rotation)
    if rotation == 0 or rotation == 360:
        pass
    elif rotation == 90:
        image = image.transpose(Image.ROTATE_90)
    elif rotation == 180:
        image = image.transpose(Image.ROTATE_180)
    elif rotation == 270:
        image = image.transpose(Image.ROTATE_270)
    else:
        image = image.rotate(rotation, expand=True)
    if image.size[0] > tecnical_properties["maxWidth"] or image.size[1] > tecnical_properties["maxHeight"]:
        aspect_ratio = image.size[0]/image.size[1]
        if tecnical_properties["maxWidth"] / tecnical_properties["maxHeight"] > aspect_ratio:
            new_width = tecnical_properties["maxWidth"]
            new_height = tecnical_properties["maxHeight"]*aspect_ratio
        else:
            new_height = tecnical_properties["maxHeight"]
            new_width = tecnical_properties["maxWidth"]/aspect_ratio
        image = image.resize((new_width, new_height))
    return image, ""


def _iiif_quality(image: Image.Image, quality:str, tecnical_properties:dict) -> Tuple[Image.Image, str]:
    if quality == "default":
        pass
    elif quality == "color":
        image = image.convert("RGB")
    elif quality == "gray":
        image = image.convert("L")
    elif quality == "bitonal":
        image = image.convert("1")
    else:
        return None, f"Quality '{quality}' not supported"
    return image, ""


def compute_iiif(pil_image, imgmd5, region, size="max", rotation="0", quality="default", format="jpg") -> Tuple[BytesIO, str] :
    #print(f"IIIF: imgmd5:{imgmd5} region:{region} size:{size} rotation:{rotation} quality:{quality} format:{format}")
    image = pil_image
    tecnical_properties = {
        "id":imgmd5,
        "type": "ImageService3",
        "protocol": "http://iiif.io/api/image",
        "profile": "level2",
        "width": image.size[0],
        "height": image.size[1],
        "maxWidth": global_max_width,
        "maxHeight": global_max_height,
        "maxArea": global_max_width * global_max_height, # FOR NOW THIS HAS TO BE THE PRODUCT OF maxWidth and maxHeight
    }
    if region == "info.json":
        return tecnical_properties, "application/json"
    
    image, error = _iiif_region(image, region, tecnical_properties)
    if error != "":
        return error, 406
    
    print(f"\n\nIIIF: imgmd5:{imgmd5} region:{region} size:{size} rotation:{rotation} quality:{quality} format:{format}\n\n")
    image, error = _iiif_size(image, size, tecnical_properties)
    if error != "":
        return error, 406

    image, error = _iiif_rotation(image, rotation, tecnical_properties)
    if error != "":
        return error, 406

    image, error = _iiif_quality(image, quality, tecnical_properties)
    if error != "":
        return error, 406

    image_stream = BytesIO()
    if format.lower() in ["jpeg", "jpg"]:
        image.save(image_stream, format="JPEG")
        image_stream.seek(0)
        mimetype = 'image/jpeg'
    elif format.lower() == "jpeg":
        image.save(image_stream, format="JPEG", quality=100)
        image_stream.seek(0)
        mimetype = 'image/jpeg'
    elif format.lower() == "png":
        image.save(image_stream, format="PNG")
        image_stream.seek(0)
        mimetype = 'image/png'
    elif format.lower() == "bmp":
        image.save(image_stream, format="BMP")
        image_stream.seek(0)
        mimetype = 'image/bmp'
    elif format.lower() == "webp":
        image.save(image_stream, "WebP")
        image_stream.seek(0)
        mimetype = 'image/webp'
    else:
        raise ValueError(f"Format '{repr(format)}' not supported")
    return image_stream, mimetype
    


load_image_from_url = lambda url: Image.open(BytesIO(requests.get(url).content))
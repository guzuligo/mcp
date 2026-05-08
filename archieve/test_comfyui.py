#!/usr/bin/env python3
"""Test script for useComfyui workflow sending."""
import json
import httpx
import time
from datetime import datetime

# Build workflow like _build_workflow does
workflow_graph = {
    "4": {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(datetime.now().timestamp()) % 10**8,
            "steps": 20,
            "cfg_scale": 7.5,
            "sampler_name": "euler_ancestral",
            "scheduler": "normal",
        },
    },
}
workflow_graph["5"] = {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": "a cat sitting on a windowsill"},
}
workflow_graph["6"] = {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": ""},
}
workflow_graph["4"]["inputs"]["positive_prompt"] = {"node": "5", "field": "text"}
workflow_graph["4"]["inputs"]["negative_prompt"] = {"node": "6", "field": "text"}
workflow_graph["7"] = {
    "class_type": "EmptyLatentImage",
    "inputs": {"width": 512, "height": 512},
}
workflow_graph["8"] = {
    "class_type": "SaveImage",
    "inputs": {
        "filename_prefix": "comfyui_output",
        "images": {"node": "4", "field": "LATENT"},
    },
}
workflow_graph["4"]["inputs"]["latent"] = {"node": "7", "field": "samples"}

base_url = "http://127.0.0.1:8188"
with httpx.Client(timeout=30) as client:
    resp = client.post(f'{base_url}/prompt', json={'prompt': workflow_graph})
    data = resp.json()
    
    if 'error' in data:
        print(f"Error from server: {json.dumps(data, indent=2)}")
    else:
        prompt_id = data.get('prompt_id')
        print(f'Submitted prompt ID: {prompt_id}')
        
        # Poll history like _send_and_wait does
        start = datetime.now()
        found_images = []
        while (datetime.now() - start).total_seconds() < 60:
            hist_resp = client.get(f'{base_url}/history')
            history = hist_resp.json()
            
            print(f'--- History keys: {list(history.keys())}')
            if prompt_id in history:
                node_data = history[prompt_id]
                outputs = node_data.get('outputs', {})
                print(f'Node data type: {type(node_data)}')
                print(f'Full node_data: {json.dumps(node_data, indent=2)[:500]}')
                
                for nid, node_output in outputs.items():
                    print(f'  Node {nid}: type={type(node_output)}, keys={list(node_output.keys()) if isinstance(node_output, dict) else "N/A"}')
                    if isinstance(node_output, dict) and 'images' in node_output:
                        for img in node_output['images']:
                            filename = img.get('filename', '')
                            subfolder = img.get('subfolder', '')
                            filepath = f'{subfolder}/{filename if subfolder else ""}'
                            found_images.append(filepath)
                
                if found_images:
                    images_str = '\n'.join(found_images)
                    print(f'FOUND IMAGES:\n{images_str}')
                    break
            
            time.sleep(1)
        else:
            print('Timed out - no output found')
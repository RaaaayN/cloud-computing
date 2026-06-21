from torchvision.models import resnet18, ResNet18_Weights
import torch
import cv2
import numpy as np
import requests

torch.set_num_interop_threads(1)
torch.set_num_threads(1)

# Labeled Catogories
url = "https://storage.googleapis.com/download.tensorflow.org/data/imagenet_class_index.json"
response = requests.get(url)
class_idx = response.json()
labels = [class_idx[str(i)][1] for i in range(1000)]

# Load the pre-trained ResNet-18 model
model = resnet18(weights=ResNet18_Weights.DEFAULT)
model.eval()
model = model.to('cpu')

# # Preprocess the image
# im = cv2.resize(im, (224, 224))
# im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
# im = im.transpose((2, 0, 1))  # Change data layout from HWC to CHW
# im = im / 255.0  # Normalize to [0, 1]
# im = torch.tensor(im, dtype=torch.float32)
# im = im.unsqueeze(0)  # Add batch dimension
# im = im.half()  # Convert to half precision
# # im = im.to('cuda')  # Move to GPU


# # # Run inference
# # with torch.no_grad():
# output = model(im)
# # print(output)


# # Post-process the output
# _, predicted = torch.max(output, 1)
# # print(predicted)

# # Print the predicted class
# print(f'Predicted class: {predicted.item()}')

# # Get the corresponding label
# predicted_label = labels[predicted.item()]
# print(f"Predicted label: {predicted_label}")


def get_prediction(img_bytes):
    arr = np.frombuffer(img_bytes, np.uint8)
    im = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    im = cv2.resize(im, (224, 224))
    im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
    im = im.transpose((2, 0, 1))  # Change data layout from HWC to CHW
    im = im / 255.0  # Normalize to [0, 1]
    im = torch.tensor(im, dtype=torch.float32)
    im = im.unsqueeze(0)  # Add batch dimension

    with torch.no_grad():
        output = model(im)

    # Post-process the output
    _, predicted = torch.max(output, 1)

    # Get the corresponding label
    predicted_label = labels[predicted.item()]
    return predicted_label

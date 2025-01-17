# -*- coding: utf-8 -*-
"""Dandelion-Grass

### **Installing ColossalAI**
"""

! pip install deepspeed colossalai

"""### **Importing Required Modules**"""

import pandas as pd
import os
from torch.utils.data import Dataset
import pandas as pd
import os
from PIL import Image
import torch
import colossalai
from colossalai.engine import Engine, NoPipelineSchedule
from colossalai.trainer import Trainer
from colossalai.context import Config
import torch.nn as nn
import torchvision.models as models
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
import torch.optim as optim
import random
import matplotlib.pyplot as plt
import cv2

"""### **Loading Data**
We uploaded a zip file called `train.zip` which contains all images and their name starts with 'dandelion' if it is a dandelion and grass if the name starts with 'grass'. Then we unzip and create a DataFrame of the images with their label.
"""

!unzip -q train.zip

main_dir = "/content/train/"
picture_data = pd.DataFrame(columns=["img_name","label"])
picture_data["img_name"] = os.listdir(main_dir)
for idx, i in enumerate(os.listdir(main_dir)):
    if i.startswith("dandelion"):
        picture_data["label"][idx] = 0
    if i.startswith("grass"):
        picture_data["label"][idx] = 1

picture_data.to_csv (r'train_csv.csv', index = False, header=True)

picture_data

import numpy as np

class dand_and_grass(Dataset):
    def __init__(self, root_dir, dataframe, transform=None):
        self.root_dir = root_dir
        self.annotations = pd.read_csv(dataframe)
        self.transform = transforms 

    def __len__(self):
      return len(self.annotations)

    def __getitem__(self, index):
        img_id = self.annotations.iloc[index, 0]
        img = Image.open(os.path.join(self.root_dir, img_id)).convert("RGB")
        y_label = torch.tensor(float(self.annotations.iloc[index, 1]))
       
        
        if self.transform is not None:
            img = self.transform(img)
        img = np.asarray(np.copy(img), dtype='float32')
        label = np.asarray(np.copy(y_label), dtype='float32')
        img = np.expand_dims(img, axis=0)
        img = torch.from_numpy(img)
        label = torch.from_numpy(label)
        targets = label.view(1)
        return (img, targets)

"""Applying some transformations on images and then dividing it in Training and Validation Set."""

transform = transforms.Compose(
        [
            transforms.Resize((356, 356)),
            # transforms.Resize((224, 224)),
            transforms.RandomCrop((299, 299)),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )
dataset = dand_and_grass(main_dir,"train_csv.csv",transform=transform)

train_set, validation_set = torch.utils.data.random_split(dataset,[900, 99])
train_loader = DataLoader(dataset=train_set, shuffle=True, batch_size=1,num_workers=1,pin_memory=True)
validation_loader = DataLoader(dataset=validation_set, shuffle=True, batch_size=1,num_workers=1, pin_memory=True)

"""### **Displaying Some Images**"""

p = list(os.listdir(main_dir))
random.shuffle(p)
examples = p[:4]
print(examples)
fig = plt.figure(figsize=(10, 7))
  
# setting values to rows and column variables
rows = 2
columns = 2
  
# reading images
Image1 = cv2.imread(main_dir + examples[0])
Image2 = cv2.imread(main_dir + examples[1])
Image3 = cv2.imread(main_dir + examples[2])
Image4 = cv2.imread(main_dir + examples[3])

Image1 = cv2.cvtColor(Image1, cv2.COLOR_BGR2RGB)
Image2 = cv2.cvtColor(Image2, cv2.COLOR_BGR2RGB)
Image3 = cv2.cvtColor(Image3, cv2.COLOR_BGR2RGB)
Image4 = cv2.cvtColor(Image4, cv2.COLOR_BGR2RGB)
  
# Adds a subplot at the 1st position
fig.add_subplot(rows, columns, 1)
  
# showing image
plt.imshow(Image1)
plt.axis('off')
plt.title(examples[0])
  
# Adds a subplot at the 2nd position
fig.add_subplot(rows, columns, 2)
  
# showing image
plt.imshow(Image2)
plt.axis('off')
plt.title(examples[1])
  
# Adds a subplot at the 3rd position
fig.add_subplot(rows, columns, 3)
  
# showing image
plt.imshow(Image3)
plt.axis('off')
plt.title(examples[2])
  
# Adds a subplot at the 4th position
fig.add_subplot(rows, columns, 4)
  
# showing image
plt.imshow(Image4)
plt.axis('off')
plt.title(examples[1])

"""### **Defining Model**

We are using transfer learning. We are freezing all the layers of pre-trained InceptionV3 model except the last one.
"""

class CNN(nn.Module):
    def __init__(self, train_CNN=False, num_classes=1):
        super(CNN, self).__init__()
        self.train_CNN = train_CNN
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(0.5)
        self.sigmoid = nn.Sigmoid()
        self.inception = models.inception_v3(pretrained=True, aux_logits=False)
        self.inception.fc = nn.Linear(self.inception.fc.in_features, num_classes)
        
    def forward(self, images):
        features = self.inception(images)
        return self.sigmoid(self.dropout(self.relu(features))).squeeze(1)
model = CNN().cuda()


for name, param in model.inception.named_parameters():
    if "fc.weight" in name or "fc.bias" in name:
        param.requires_grad = True
    else:
        param.requires_grad = False

"""### **Initialising Distributed  Environment**"""

parallel_cfg = Config(dict(parallel=dict(
    data=dict(size=1),
    pipeline=dict(size=1),
    tensor=dict(size=1, mode=None),
)))
colossalai.init_dist(config=parallel_cfg,
          local_rank=0,
          world_size=1,
          host='127.0.0.1',
          port=8888,
          backend='nccl')

"""### **Defining Loss and Optimisation functions**
### **Initialising Engine and Trainer**
"""

criterion = nn.BCELoss()
optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9)
schedule = NoPipelineSchedule()

engine = Engine(
        model=model,
        criterion=criterion,
        optimizer=optimizer,
        lr_scheduler=None,
        schedule=schedule
    )
trainer = Trainer(engine=engine,
          hooks_cfg=[dict(type='LossHook'), dict(type='LogMetricByEpochHook'), dict(type='AccuracyHook')],
          verbose=True)

"""### **Finally Training**"""

num_epochs = 10
test_interval = 1
trainer.fit(
        train_dataloader=train_loader,
        test_dataloader=validation_loader,
        max_epochs=num_epochs,
        display_progress=True,
        test_interval=test_interval
    )

"""And here we get 89% accuracy in only 18 minutes of training because of ColossalAI"""

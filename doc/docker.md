### This document was created in 5/7/2022
### Used guides:
[Ubuntu 10.04 docker tutorial](https://www.digitalocean.com/community/tutorials/how-to-install-and-use-docker-on-ubuntu-20-04)
[NVIDIA Docker 4 min video](https://www.youtube.com/watch?v=-Y4T71UDcMY)
[NVIDIA Docker repository](https://github.com/NVIDIA/nvidia-docker)

### Usefull commands:

* Add user to docker: (only admin can do that)
```bash
sudo usermod -aG docker ${USER}
```

* Check if your user is in docker group (you might need to logout)
```bash
groups # or alternatevelly id
```
docker should be one of the outputs

* Can you run docker successfully?
```bash
docker run hello-world
```
If you see a few lines on stdout including one with "Hello from Docker!" you are able to run docker.


* Can you access the GPU in side docker?
```bash
docker run --rm --gpus all nvidia/cuda:11.0.3-base-ubuntu20.04 nvidia-smi
```

* Lauch interactive python with pytorch
```bash
docker run -it -u $(id -u):$(id -g) --rm --gpus all pytorch/pytorch python -m IPython
```

* Launch interactive python with tensorflow
```bash
docker run -it -u $(id -u):$(id -g) --gpus all --rm tensorflow/tensorflow:latest-gpu-jupyter jupyter notebook
```

```bash
docker run -it -u $(id -u):$(id -g) --rm --gpus all tensorflow/tensorflow:latest-gpu-jupyter bash
```

```bash
# after you get your bash promt:
mkdir /tmp/home
export HOME=/tmp/home ; export PATH="$PATH:$HOME/.local/bin"
pip install ipython
ipython
```

### Instalation

1. Install NVIDIA/Cuda

Installing as a 

2. Install Docker

3. Install NVIDIA Container Toolkit

```bash
distribution=$(. /etc/os-release;echo $ID$VERSION_ID) \
      && curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
      && curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
```

This did not work out of the box so we had to update the keys
```bash
sudo apt-key del 7fa2af80
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-keyring_1.0-1_all.deb
sudo dpkg -i cuda-keyring_1.0-1_all.deb
```

```bash
sudo apt-get install -y nvidia-docker2
```


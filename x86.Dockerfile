FROM node:14-alpine as frontend
WORKDIR /frontend
COPY ui/frontend/package.json ui/frontend/package-lock.json /frontend/
RUN npm install --production
COPY ui/frontend /frontend
RUN npm run build

FROM tensorflow/tensorflow:latest-gpu-py3

ARG OPENCV_VERSION=4.3.0

VOLUME  /repo
WORKDIR /repo/applications/smart-distancing

# get all packages from apt sources
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake \
        g++ \
        gcc \
        git \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-vaapi \
        libavcodec-dev \
        libavformat-dev \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer1.0-dev \
        libsm6 \
        libswscale-dev \
        libxext6 \
        libxrender-dev \
        mesa-va-drivers \
        pkg-config \
        python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# download and build OpenCv
RUN cd /tmp/ \
    && curl -L https://github.com/opencv/opencv/archive/${OPENCV_VERSION}.tar.gz -o opencv.tar.gz \
    && tar zxvf opencv.tar.gz && rm opencv.tar.gz \
    && cd /tmp/opencv-${OPENCV_VERSION} \
    && mkdir build \
    && cd build \
    && cmake -DBUILD_opencv_python3=yes -DPYTHON_EXECUTABLE=/usr/local/bin/python3 ../ \
    && make -j$(nproc) \
    && make install \
    && cd /tmp \
    && rm -rf opencv-${OPENCV_VERSION}

# get all pip packages
RUN pip install --upgrade pip setuptools==41.0.0 && pip install \
    aiofiles \
    fastapi \
    image \
    scipy \
    uvicorn \
    wget

COPY --from=frontend /frontend/build /srv/frontend

EXPOSE 8000

ENTRYPOINT ["python", "neuralet-distancing.py"]
CMD ["--config", "config-x86.ini"]

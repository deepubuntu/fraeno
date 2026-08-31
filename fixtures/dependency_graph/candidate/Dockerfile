FROM --platform=linux/amd64 ros@sha256:2a7ca548c7f0f87bc6393ee161dea3283e1c6fa280916f8944b1afadde2d26ec AS builder
RUN apt-get update && apt-get install -y \
    libopencv-dev=4.8.0+dfsg-5

FROM --platform=linux/amd64 ros:humble-ros-base-jammy AS runtime
RUN apt-get update && apt-get install -y libopencv-dev=4.8.0+dfsg-5

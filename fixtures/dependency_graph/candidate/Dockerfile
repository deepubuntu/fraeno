FROM --platform=linux/amd64 ros:humble-ros-base-jammy AS builder
RUN apt-get update && apt-get install -y \
    libopencv-dev=4.8.0+dfsg-5

FROM --platform=linux/amd64 ros:humble-ros-base-jammy AS runtime
RUN apt-get update && apt-get install -y libopencv-dev=4.8.0+dfsg-5

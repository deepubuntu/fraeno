FROM --platform=linux/amd64 ros:humble-ros-base-jammy AS builder
RUN apt-get update && apt-get install -y \
    libopencv-dev=4.5.4+dfsg-9ubuntu4

FROM --platform=linux/amd64 ros:humble-ros-base-jammy AS runtime
RUN apt-get update && apt-get install -y libopencv-dev=4.5.4+dfsg-9ubuntu4

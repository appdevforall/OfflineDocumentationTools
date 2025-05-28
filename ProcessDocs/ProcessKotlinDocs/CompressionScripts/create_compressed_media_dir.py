import numpy as np
import os
import subprocess
import sys
import time

def process_image(in_image, image_quality, out_dir):
    # Input file size
    orig_size = os.path.getsize(in_image)

    # Input file basename
    basename = os.path.basename(in_image)

    # Assuming no dots in file name beyond file extension extension
    no_ext = basename.split(".")[0]

    # Output filename, replace png with webp
    out_image = os.path.join(out_dir, no_ext + ".webp")

    # Collect runtime info
    t_0 = time.perf_counter()

    # cwebp -near_lossless <quality> <input file> -o <output file>
    command = "cwebp -near_lossless " + str(image_quality) + " " + in_image + " -o " + out_image
    process = subprocess.run(command, shell=True, capture_output=True, text=True)

    t_1 = time.perf_counter()

    runtime = t_1 - t_0

    compressed_size = os.path.getsize(out_image)

    return orig_size, compressed_size, runtime


def process_image_batch(images, image_quality, out_dir):
    image_stats = {}

    for image in images:
        image_stats[image] = {}
        orig_size, compressed_size, runtime = process_image(image, image_quality, out_dir)

        image_stats[image]["orig_size"] = orig_size
        image_stats[image]["compressed_size"] = compressed_size
        image_stats[image]["runtime"] = runtime

    return image_stats


def run_compression(in_dir, out_dir, image_quality):
    images = [os.path.join(in_dir, f) for f in os.listdir(in_dir) if ".png" in f]

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    image_stats = process_image_batch(images, image_quality, out_dir)

    return image_stats

def main():
    print("a")

if __name__ == '__main__':
    main()
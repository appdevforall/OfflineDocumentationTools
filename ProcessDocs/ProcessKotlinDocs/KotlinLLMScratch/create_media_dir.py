#!/usr/bin/env python3

import os
import sys
import subprocess
import time
import shutil
import argparse

def process_image(in_image, image_quality, out_dir):
    # Input file size
    orig_size = os.path.getsize(in_image)

    # Input file basename
    basename = os.path.basename(in_image)

    # Assuming no dots in file name beyond file extension
    no_ext = basename.split(".")[0]

    # Output filename: change extension to .webp
    out_image = os.path.join(out_dir, no_ext + ".webp")

    # Collect runtime info
    t_0 = time.perf_counter()

    # Build command for lossy conversion using cwebp
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
    # Get only PNG files
    images = [os.path.join(in_dir, f) for f in os.listdir(in_dir) if f.lower().endswith('.png')]

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    image_stats = process_image_batch(images, image_quality, out_dir)
    return image_stats

def copy_non_png_files(in_dir, out_dir):
    # Iterate over all files in the input directory
    for f in os.listdir(in_dir):
        input_path = os.path.join(in_dir, f)
        # If it is a file and not a PNG (we assume PNG files end with .png, case insensitive),
        # then copy it into the output directory.
        if os.path.isfile(input_path) and not f.lower().endswith('.png'):
            output_path = os.path.join(out_dir, f)
            shutil.copy2(input_path, output_path)

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Convert PNG images to WEBP, and copy non-PNG files.")
    parser.add_argument("--input-dir", required=True, help="Input directory containing the image files")
    parser.add_argument("--output-dir", required=True, help="Output directory to place WEBP images and copies of non-PNG files")
    parser.add_argument("--quality", type=int, required=True, help="Quality level for lossy PNG-to-WEBP conversion using cwebp")
    args = parser.parse_args()

    in_dir = args.input_dir
    out_dir = args.output_dir
    quality = args.quality

    # Check if input directory exists
    if not os.path.exists(in_dir):
        print("Error: Input directory does not exist.")
        sys.exit(1)

    # Create the output directory if necessary
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # Process the PNG images using the helper functions.
    print("Converting PNG files to WEBP...")
    png_stats = run_compression(in_dir, out_dir, quality)
    for image, stats in png_stats.items():
        print(f"{os.path.basename(image)}: original size = {stats['orig_size']} bytes, WEBP size = {stats['compressed_size']} bytes, conversion time = {stats['runtime']:.3f} seconds")

    # Copy the non-PNG files to the output directory.
    print("Copying non-PNG files...")
    copy_non_png_files(in_dir, out_dir)

    print("Processing complete.")

if __name__ == '__main__':
    main()

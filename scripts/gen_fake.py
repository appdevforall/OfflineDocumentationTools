from faker import Faker
import os
import random

fake = Faker()
output_dir = "fake_files"

if not os.path.exists(output_dir):
    os.makedirs(output_dir)

file_types = ["txt", "md", "html", "css", "png", "jpg"]

for i in range(100):
    file_type = random.choice(file_types)
    filename = f"fake_file_{i+1}.{file_type}"
    filepath = os.path.join(output_dir, filename)

    if file_type == "txt":
        content = fake.paragraph(nb_sentences=random.randint(5, 15))
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Generated: {filename} (text)")
    elif file_type == "md":
        title = fake.sentence()
        paragraphs = [fake.paragraph(nb_sentences=random.randint(3, 8)) for _ in range(random.randint(2, 5))]
        content = f"# {title}\n\n" + "\n\n".join(paragraphs)
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Generated: {filename} (markdown)")
    elif file_type == "html":
        title = fake.sentence()
        body = "\n    ".join([f"<p>{fake.paragraph(nb_sentences=random.randint(2, 5))}</p>" for _ in range(random.randint(3, 7))])
        content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
</head>
<body>
    {body}
</body>
</html>"""
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Generated: {filename} (html)")
    elif file_type == "css":
        selectors = [fake.word() for _ in range(random.randint(2, 5))]
        rules = []
        content = ""  # Initialize content variable
        for selector in selectors:
            num_rules = random.randint(1, 3)
            for _ in range(num_rules):
                property_name = random.choice(["color", "font-size", "margin", "padding", "background-color"])
                property_value = fake.word() if property_name in ["color"] else f"{random.randint(10, 20)}px"
                rules.append(f"  {property_name}: {property_value};")
            content += f"{selector} {{\n" + "\n".join(rules) + "\n}\n\n"
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Generated: {filename} (css)")
    elif file_type == "png" or file_type == "jpg":
        # For image files, we'll just create an empty file for simplicity
        open(filepath, 'a').close()
        print(f"Generated: {filename} (empty image file)")

print(f"\nSuccessfully generated 100 fake files in the '{output_dir}' directory.")

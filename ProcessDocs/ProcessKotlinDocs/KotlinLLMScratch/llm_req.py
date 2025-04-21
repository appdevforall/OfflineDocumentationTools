from openai import OpenAI
import time
key = open("openaikey.txt", "r").read()
prompt_file = "kotlin_full_prompt.txt"
prompt = open(prompt_file, "r").read()

client = OpenAI(
    api_key=key,  # This is the default and can be omitted
)

completion = client.chat.completions.create(
    model="o3-mini",
    messages=[
        {
            "role": "user",
            "content": prompt
        }
    ]
)

message = completion.choices[0].message.content
open(prompt_file + " " + str(time.time()) + ".txt", "w").write(prompt + "\n\nRESPONSE\n\n" + message)
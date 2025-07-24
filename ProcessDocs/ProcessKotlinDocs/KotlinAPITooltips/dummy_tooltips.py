#tag^category^summary^detail^buttonDescr1^buttonURI1^buttonDescr2^buttonURI2^buttonDescr3^buttonURI3
# 1^ide^Code on the Go is an integrated development environment (IDE) to build Android apps that runs on Android phones and doesn't need a connection to the Internet.^If you already know how to code using Java or Kotlin, Code on the Go lets  you develop and compile Android apps without requiring you to go online.  Our goal is to make computer science education and programming resources globally available.^Learn more about Code on the Go^getstarted_top.html^^^^


import json
import os

def tooltips_pages_json(filename):
    entries = json.loads(open(filename, "r").read())["elements"]

    tooltips_out_file = "dummy_kotlin_stdlib_tooltips.csv"
    tooltips_txt = "tag^category^summary^detail^buttonDescr1^buttonURI1^buttonDescr2^buttonURI2^buttonDescr3^buttonURI3\n"
    for entry in entries:
        tag = entry["searchKeys"][0]
        category = "kotlin"
        summary = "Placeholder tier 1"
        detail = "Placeholder tier 2"
        buttonDesc1 = "Learn more about " + tag
        buttonURI1 = "http://localhost:6174/KotlinStdlib/kotlin/" + entry["location"]
        buttonDesc2 = ""
        buttonURI2 = ""
        buttonDesc3 = ""
        buttonURI3 = ""

        tooltip = "^".join([tag, category, summary, detail, buttonDesc1, buttonURI1, buttonDesc2, buttonURI2, buttonDesc3, buttonURI3])
        tooltips_txt += tooltip + "\n"

    return tooltips_txt

def disamb_tooltips(filename):
    tooltips_txt = "tag^category^summary^detail^buttonDescr1^buttonURI1^buttonDescr2^buttonURI2^buttonDescr3^buttonURI3\n"


    for entry in open(filename, "r").readlines()[1:]:
        entry = entry.strip()
        disamb = False
        if "One meaning" in entry:
            symbol = entry.split("\t")[1]
            url = entry.split("\t")[-1]
        else:
            print(entry)
            disamb = True
            symbol = entry.split("\t")[1]
            url = entry.split("\t")[2]

        tag = symbol
        category = "kotlin"
        summary = "Placeholder tier 1 for symbol " + tag
        detail = "Placeholder tier 2 for symbol"
        if disamb:
            buttonDesc1 = "See documentation for the Kotlin Standard Library symbols with names matching " + tag
        else:
            buttonDesc1 = "Learn more about " + tag + " in the Kotlin Standard Library"
        buttonURI1 = "/kotlin-stdlib/" + url
        buttonDesc2 = ""
        buttonURI2 = ""
        buttonDesc3 = ""
        buttonURI3 = ""

        tooltip = "^".join([tag, category, summary, detail, buttonDesc1, buttonURI1, buttonDesc2, buttonURI2, buttonDesc3, buttonURI3])
        tooltips_txt += tooltip + "\n"

    return tooltips_txt

def main():
    tooltips_txt = tooltips_pages_json("pages.json")
    tooltips_out_file = "empty_tooltips.txt"

    open(tooltips_out_file, "w").write(tooltips_txt)

    kotlin_tooltips = disamb_tooltips("log.txt")
    open("kotlin_tooltips.tsv", "w").write(kotlin_tooltips)

if __name__ == "__main__":
    main()
import os
import json
import matplotlib.pyplot as plt

TOOLTIPS_JSON = "pages.json"

def main():
    api_data = json.loads(open(TOOLTIPS_JSON, 'r').read())



    encountered_names = {}
    encountered_descs = {}

    desc_count = 0

    stdlib_entries = api_data["elements"]
    disc_count = 0
    keep_count = 0

    urls = set()

    for entry in stdlib_entries:
        name = entry["searchKeys"][0]
        desc = entry["description"]

        urls.add(entry["location"])

        if "kotlin.test" in desc or "kotlin.reflect" in desc:
            desc_count += 1
            continue
        else:
            keep_count += 1

        if desc not in encountered_descs:
            encountered_descs[desc] = 1
        else:
            encountered_descs[desc] += 1


        if name not in encountered_names:
            encountered_names[name] = 1
        else:
            encountered_names[name] += 1


    counts_file = "dup_count.txt"
    counts_txt = "symbol\tcount\n"

    desc_counts_file = "dup_desc_count.txt"
    desc_counts_txt = "desc\tcount\n"

    uniq_count = 0

    uniq_desc_count = 0

    for name, count in encountered_descs.items():
        if count > 1:
            desc_counts_txt += name + "\t" + str(count) + "\n"
        else:
            uniq_desc_count += 1

    for name, count in encountered_names.items():
        if count > 1:
            counts_txt += name + "\t" + str(count) + "\n"
        else:
            uniq_count += 1

    open(counts_file, "w").write(counts_txt)
    open(desc_counts_file, "w").write(desc_counts_txt)
    print("Unique symbols: " + str(uniq_count))
    print("Unique classes/methods (description): " + str(uniq_desc_count))
    print("Total API entries: " + str(keep_count))
    print("Discarded test/reflect entries: " + str(desc_count))

    print("Unique page refs: " + str(len(urls)))

    all_counts = [e[1] for e in encountered_names.items()]
    all_counts_dup = [c for c in all_counts if c > 1]
    plt.hist(all_counts_dup, bins=[20 * i for i in range(9)])
    print("Most duplicated term count: " + str(max(all_counts)))

    all_counts = [e[1] for e in encountered_descs.items()]
    all_counts_dup = [c for c in all_counts if c > 1]
   # plt.hist(all_counts_dup, bins=[20 * i for i in range(9)])
    print("Most duplicated desc count: " + str(max(all_counts)))
    #plt.savefig('dup_counts_hist.png')

if __name__ == '__main__':
    main()
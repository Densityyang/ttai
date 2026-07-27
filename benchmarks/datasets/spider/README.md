# Spider Dev Set

Spider 1.0 dev set needs to be downloaded manually due to network restrictions.

## Download Instructions

On the Linux VM with proper network access, run:

```bash
# Option 1: wget from Yale
wget -O spider.zip "https://yale-lily.github.io/spider/spider.zip"
unzip spider.zip
mv spider/dev.json .
mv spider/tables.json .
rm -rf spider spider.zip

# Option 2: git clone
git clone https://github.com/taoyds/spider.git
mv spider/dev.json .
mv spider/tables.json .
rm -rf spider
```

Or use the download script at `benchmarks/download_datasets.sh`.

## Spider 2.0 (Optional)

For the more challenging Spider 2.0:
```bash
git clone https://github.com/xlang-ai/Spider2.git
```

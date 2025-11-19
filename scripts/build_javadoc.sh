# change "jsource" to the root directory of your copy of the Java sources
# change "testout" to the output directory where you want to place all of the

MODULES=$(find jsource -maxdepth 1 -type d -printf "%f," | sed 's/jsource,//' | sed 's/,$//')

javadoc \
  --module-source-path jsource \
  --module $MODULES \
  -d testout

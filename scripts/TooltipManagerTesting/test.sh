python3 ../TooltipManager.py --operation build --name Alex --input-csv initial_db.csv --input-db empty.db --output-db initial.db
python3 ../TooltipManager.py --operation dump --name Alex --input-db initial.db --output-csv initial_res.csv

python3 ../TooltipManager.py --operation build --name Alex --input-csv modify_added.csv --input-db initial.db --output-db added.db
python3 ../TooltipManager.py --operation dump --name Alex --input-db added.db --output-csv added_res.csv

python3 ../TooltipManager.py --operation build --name Alex --input-csv modify_deleted.csv --input-db initial.db --output-db deleted.db
python3 ../TooltipManager.py --operation dump --name Alex --input-db deleted.db --output-csv deleted_res.csv

python3 ../TooltipManager.py --operation build --name Alex --input-csv modify_updated.csv --input-db initial.db --output-db updated.db
python3 ../TooltipManager.py --operation dump --name Alex --input-db updated.db --output-csv updated_res.csv
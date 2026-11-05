"""Standalone pure extraction from the existing importer."""
from __future__ import annotations


TEXT_FIELDS = ('product_type', 'brand', 'manufacturer', 'manufacturer_article', 'raw_material',
               'grade', 'composition_raw', 'standard_raw', 'shelf_life_raw', 'shelf_life_unit',
               'storage_conditions_raw', 'consumer_packaging', 'transport_packaging', 'unit_count_method')


NUMBER_FIELDS = ('fat_pct', 'shelf_life_value', 'unit_mass_g', 'unit_mass_min_g', 'unit_mass_max_g',
                 'unit_volume_ml', 'unit_volume_min_ml', 'unit_volume_max_ml', 'units_per_box')


ALIASES = {
    'brand': {'бренд', 'торговая марка', 'тм'},
    'manufacturer': {'производитель', 'изготовитель'},
    'manufacturer_article': {'артикул производителя', 'артикул изготовителя'},
    'raw_material': {'сырье', 'вид сырья'},
    'grade': {'сорт', 'категория', 'класс'},
    'composition_raw': {'состав', 'состав ru'},
    'standard_raw': {'гост', 'ту', 'гост/ту', 'нормативный документ'},
    'storage_conditions_raw': {'температура хранения', 'температурный режим', 'темп. режим', 'условия хранения', 'хранение', 'температура и срок хранения'},
    'shelf_life_raw': {'срок годности', 'сроки годности', 'срок хранения', 'срок хранения/ реализации', 'срок хранения, в днях', 'срок хранения в днях', 'срок годности, сут.', 'срок годности (дни)', 'срок годности, мес.'},
}


def key(x):
    return ' '.join(str(x or '').replace('\u00a0', ' ').casefold().replace('ё', 'е').split())

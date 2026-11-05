"""Synthetic XLSX only. No operational data or network is used."""
import argparse
from pathlib import Path
from openpyxl import Workbook


def create_demo(path: Path) -> Path:
    if path.exists():
        raise FileExistsError('demo already exists')
    book = Workbook()
    first = book.active
    assert first is not None
    first.title = 'С явной ставкой'
    first.append(['Синтетический демонстрационный прайс, не коммерческое предложение'])
    first.merge_cells('A1:E1')
    first.append(['Артикул', 'Наименование', 'Цена за шт с НДС', 'НДС', 'Примечание'])
    first.append(['0001', 'Творог 0% 200 г', 110, '10%', 'Явная ставка в отдельной колонке'])
    first.append(['0002', 'Соль пищевая 1 кг', 80, '0%', 'Нулевая ставка указана явно, только тест'])
    first.append([None, 'Раздел: демонстрационные позиции'])
    second = book.create_sheet('Без указанной ставки')
    second.append(['Артикул', 'Наименование', 'Цена за шт с НДС', 'Примечание'])
    second.append(['0003', 'Творог 0% 200 г', 125, '0% в названии относится к жирности, не к НДС'])
    second.append(['0004', 'Соль пищевая 1 кг', None, 'Нет цены: товар сохраняется'])
    book.save(path)
    book.close()
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path, nargs='?', default=Path('demo.xlsx'))
    args = parser.parse_args()
    print(create_demo(args.out))

from datetime import datetime
import pandas as pd
import xlwt

def parse_single_export_file(file_path: str):
    """
    精确解析 GMS 导出的单档位礼包码 Excel 文件
    原始表格结构：
      Row 0: 标题（例如 ['激活码', '', ...]）
      Row 1: 表头 ['批次号', '激活码', '状态', '创建时间', '服务器名称', '角色ID', '角色名', '领取时间', '注册天数限制', '失效时间']
      Row 2+: 数据行
    提取目标列：激活码 (第 1 列), 创建时间 (第 3 列), 失效时间 (第 9 列)
    """
    records = []
    
    # 1. 优先使用 xlrd 处理标准的 .xls 文件
    if file_path.endswith('.xls') or not file_path.endswith('.csv'):
        try:
            import xlrd
            wb = xlrd.open_workbook(file_path)
            sheet = wb.sheet_by_index(0)
            
            header_row_idx = None
            code_idx = 1
            create_idx = 3
            expire_idx = 9
            
            # 扫描前 10 行寻找包含“批次号”或“激活码”的真实表头行
            for r in range(min(10, sheet.nrows)):
                row_vals = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
                if '批次号' in row_vals and '激活码' in row_vals:
                    header_row_idx = r
                    for c_idx, val in enumerate(row_vals):
                        if val == '激活码':
                            code_idx = c_idx
                        elif val == '创建时间':
                            create_idx = c_idx
                        elif val in ['失效时间', '过期时间', '截止时间']:
                            expire_idx = c_idx
                    break
                    
            if header_row_idx is not None:
                for r in range(header_row_idx + 1, sheet.nrows):
                    code = str(sheet.cell_value(r, code_idx)).strip()
                    c_time = str(sheet.cell_value(r, create_idx)).strip()
                    e_time = str(sheet.cell_value(r, expire_idx)).strip()
                    if code and code != '激活码':
                        if e_time and len(e_time) == 10 and '-' in e_time:
                            e_time = f"{e_time} 00:00:00"
                        records.append({
                            'code': code,
                            'created_time': c_time,
                            'expired_time': e_time
                        })
                if records:
                    return records
        except Exception as e:
            pass
            
    # 2. 兜底使用 pandas 逐行扫描匹配
    try:
        df = None
        for skip in [1, 0, 2]:
            try:
                temp_df = pd.read_excel(file_path, header=skip)
                if '批次号' in temp_df.columns and '激活码' in temp_df.columns:
                    df = temp_df
                    break
            except Exception:
                pass
                
        if df is None:
            df = pd.read_excel(file_path)
            
        code_col = None
        create_col = None
        expire_col = None
        for c in df.columns:
            c_str = str(c).strip()
            if c_str == '激活码':
                code_col = c
            elif c_str == '创建时间':
                create_col = c
            elif c_str in ['失效时间', '过期时间']:
                expire_col = c
                
        for _, row in df.iterrows():
            code = str(row[code_col]).strip() if code_col else ''
            c_time = str(row[create_col]).strip() if create_col else ''
            e_time = str(row[expire_col]).strip() if expire_col else ''
            if code and code != 'nan' and code != '激活码':
                if e_time and len(e_time) == 10 and '-' in e_time:
                    e_time = f"{e_time} 00:00:00"
                records.append({
                    'code': code,
                    'created_time': c_time,
                    'expired_time': e_time
                })
    except Exception:
        pass
        
    return records


def validate_records(tier, records, count, expiry):
    if len(records) != count:
        raise ValueError(f"{tier} 档预期 {count} 个礼包码，实际导出 {len(records)} 个")
    codes = {str(row["code"]).strip() for row in records}
    if "" in codes or len(codes) != count:
        raise ValueError(f"{tier} 档存在空码或重复码")
    for row in records:
        try:
            datetime.fromisoformat(row["created_time"].strip())
            actual = datetime.fromisoformat(row["expired_time"].strip())
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"{tier} 档导出时间缺失或格式错误") from exc
        if actual != datetime.fromisoformat(expiry):
            raise ValueError(f"{tier} 档导出失效时间 {actual} 与设定的 {expiry} 不一致")


def write_xls(directory, name, results, count, expiry):
    from unified_parser import TIERS

    if set(results) != set(TIERS):
        raise ValueError("无法交付缺少档位的汇总表")
    book = xlwt.Workbook()
    header = xlwt.easyxf("font: bold on; align: horiz center;")
    seen = set()
    for tier in TIERS:
        records = results[tier]
        validate_records(tier, records, count, expiry)
        sheet = book.add_sheet(tier)
        for column, (title, width) in enumerate(zip(("激活码", "创建时间", "失效时间"), (10240, 8704, 8704))):
            sheet.write(0, column, title, header)
            sheet.col(column).width = width
        for index, row in enumerate(records, 1):
            code = str(row["code"]).strip()
            if code in seen:
                raise ValueError(f"{tier} 档出现跨档重复的礼包码")
            seen.add(code)
            for column, value in enumerate((code, row["created_time"], row["expired_time"])):
                sheet.write(index, column, str(value))
    final_path = directory / f"{name}.xls"
    temporary = final_path.with_suffix(".tmp")
    book.save(str(temporary))
    temporary.replace(final_path)
    return str(final_path)

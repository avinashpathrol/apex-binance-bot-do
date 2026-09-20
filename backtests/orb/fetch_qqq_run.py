import fetch_data as F
F.SYMS = ['QQQUSDT', 'TQQQUSDT']
F.main() if hasattr(F, 'main') else None
open('QQQ_DONE', 'w').write('ok')

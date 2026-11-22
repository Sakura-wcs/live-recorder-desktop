STYLESHEET = """
QWidget { color: #e7eef0; background: #11191d; font: 10pt "Microsoft YaHei UI"; }
QLabel#section { font-size: 19pt; font-weight: 700; }
QLabel#caption { color: #aabec0; }
QLabel#eyebrow { color: #8ed1b2; font-weight: 700; }
QWidget#sidebar { background: #172328; border-right: 1px solid #2c4045; }
QWidget#sidebar QLabel { background: transparent; }
QPushButton { background: #25363a; border: 1px solid #3b5659; border-radius: 5px; padding: 7px 11px; }
QPushButton:hover { background: #344c50; }
QPushButton:focus { border-color: #adf0c8; }
QPushButton:disabled { color: #849398; }
QPushButton#primary { background: #a5e4be; color: #142b21; font-weight: 700; }
QPushButton#primary:hover { background: #c6f0d6; }
QPushButton#primary:disabled { background: #263a38; color: #a0acab; }
QListWidget, QTableView { border: 1px solid #31454b; background: #182428; alternate-background-color: #1c2a2f;
  selection-background-color: #315850; selection-color: #ffffff; outline: 1px solid #304147; }
QListWidget#nav { border: none; background: #172328; }
QListWidget#nav::item { padding: 10px 6px; }
QHeaderView::section { background: #23373d; color: #c7dcdd; padding: 6px; border: none; }
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox { background: #182428; border: 1px solid #3b5659; border-radius: 4px; padding: 6px; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #98d9af; }
QToolTip { color: #e7eef0; background: #172328; border: 1px solid #3b5659; }
"""

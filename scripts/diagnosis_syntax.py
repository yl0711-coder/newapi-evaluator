import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__();self.active=False;self.parts=[];self.current=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='script' and 'src' not in values and values.get('type','') in ('','module','text/javascript','application/javascript'):
            self.active=True;self.current=[]
    def handle_data(self,data):
        if self.active:self.current.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.active:self.parts.append(''.join(self.current));self.active=False


def main():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    count=0
    for name in sorted(set(names)-{''}):
        path=ROOT/name
        if not path.is_file():continue
        if path.suffix=='.py':ast.parse(path.read_text(),filename=name);count+=1
        elif path.suffix in ('.js','.cjs','.mjs'):
            subprocess.run(['node','--check',str(path)],check=True);count+=1
        elif path.suffix=='.html':
            parser=Scripts();parser.feed(path.read_text())
            for source in parser.parts:
                with tempfile.NamedTemporaryFile(suffix='.mjs',mode='w') as temp:
                    temp.write(source);temp.flush();subprocess.run(['node','--check',temp.name],check=True);count+=1
    assert count>0
    print(json.dumps({'status':'passed','checks':count,'skipped':0,'format_type_checks':'not configured in this baseline'}))


if __name__=='__main__':main()

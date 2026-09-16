"""Reuse the exact default PyMuPDF TextPage for repeated textbox readback."""


class CachedTextPage:
    def __init__(self,page):
        self.page=page
        self.textpage=page.get_textpage()

    def __getattr__(self,name):return getattr(self.page,name)

    def get_textbox(self,rect):
        return self.page.get_textbox(rect,textpage=self.textpage)


class CachedTextDocument:
    def __init__(self,document):
        self.document=document
        self.pages={}

    def __len__(self):return len(self.document)

    def __getitem__(self,index):
        if index not in self.pages:self.pages[index]=CachedTextPage(self.document[index])
        return self.pages[index]

    def __iter__(self):
        for index in range(len(self)):yield self[index]

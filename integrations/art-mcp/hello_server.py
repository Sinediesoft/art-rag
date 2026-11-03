"""最小測試工具：只用來確認 Claude 桌面版連得到你的程式（階段 2 的連線診斷）。"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="art-rag-hello")


@mcp.tool(description="連線測試工具：使用者說「用 hello 工具」時呼叫，回傳一句問候。")
def hello(name: str = "Door") -> str:
    return f"哈囉 {name}！Claude 已經透過 MCP 呼叫到你電腦上的程式了，連線是通的。"


if __name__ == "__main__":
    mcp.run()  # 用「標準輸入輸出」和 Claude 溝通，所以這個檔案裡不能用 print()

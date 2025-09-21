import discord
from discord.ext import commands
import asyncio
import os
import json
import logging
import subprocess
from dotenv import load_dotenv
import anthropic
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from collections import defaultdict, deque


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables


load_dotenv()
discord_bot_token = os.getenv('DISCORD_BOT_TOKEN')
if not discord_bot_token:
    raise RuntimeError("DISCORD_BOT_TOKEN not found in environment variables")
    
    
anthropic_key = os.getenv('ANTHROPIC_API_KEY')
if not anthropic_key:
    raise RuntimeError("ANTHROPIC_API_KEY not found in environment variables")



@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    server_name: str

class MCPClient:
    def __init__(self, server_config: Dict[str, Any]):
        self.server_config = server_config
        self.process = None
        self.tools = []
        self.server_name = server_config.get('name', 'unknown')
    
    async def start(self):
        """Start the MCP server process"""
        try:
            # If it's a Docker container
            if 'docker' in self.server_config:
                docker_config = self.server_config['docker']
                cmd = [
                    'docker', 'run', '--rm', '-i',
                    '--name', f"mcp_{self.server_name}",
                ]
                
                # # Add environment variables
                # if 'env' in docker_config:
                #     for key, value in docker_config['env'].items():
                #         cmd.extend(['-e', f"{key}={value}"])

               
                cmd.extend(['-e', f"DISCORD_BOT_TOKEN={discord_bot_token}"])
                
                # Add volumes
                if 'volumes' in docker_config:
                    for volume in docker_config['volumes']:
                        cmd.extend(['-v', volume])
                
                cmd.append(docker_config['image'])
                
                # Add command args if specified
                if 'args' in docker_config:
                    cmd.extend(docker_config['args'])
            
            # If it's a regular command
            elif 'command' in self.server_config:
                cmd = self.server_config['command']
                if isinstance(cmd, str):
                    cmd = cmd.split()
            
            else:
                raise ValueError("Server config must have either 'docker' or 'command' key")
            
            logger.info(f"Starting MCP server {self.server_name}")
            
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Initialize MCP protocol
            await self._initialize()
            
            # Get available tools
            await self._get_tools()
            
            logger.info(f"MCP server {self.server_name} started with {len(self.tools)} tools")
            
        except Exception as e:
            logger.error(f"Failed to start MCP server {self.server_name}: {e}")
            raise
    
    async def _send_request(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to the MCP server"""
        if not self.process:
            raise RuntimeError("MCP server not started")
        
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method
        }
        if params:
            request["params"] = params
        
        request_json = json.dumps(request) + '\n'
        
        try:
            self.process.stdin.write(request_json.encode())
            await self.process.stdin.drain()
            
            response_line = await self.process.stdout.readline()
            response = json.loads(response_line.decode().strip())
            
            if "error" in response:
                raise RuntimeError(f"MCP server error: {response['error']}")
            
            return response.get("result", {})
        
        except Exception as e:
            logger.error(f"Error communicating with MCP server {self.server_name}: {e}")
            raise
    
    async def _initialize(self):
        """Initialize the MCP connection"""
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "clientInfo": {
                "name": "discord-bot",
                "version": "1.0.0"
            }
        })
    
    async def _get_tools(self):
        """Get available tools from the MCP server"""
        try:
            result = await self._send_request("tools/list")
            tools_data = result.get("tools", [])
            
            self.tools = [
                MCPTool(
                    name=tool["name"],
                    description=tool["description"],
                    input_schema=tool["inputSchema"],
                    server_name=self.server_name
                )
                for tool in tools_data
            ]
        except Exception as e:
            logger.error(f"Failed to get tools from {self.server_name}: {e}")
            self.tools = []
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool on the MCP server"""
        try:
            result = await self._send_request("tools/call", {
                "name": tool_name,
                "arguments": arguments
            })
            return result
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}")
            return {"error": str(e)}
    
    async def stop(self):
        """Stop the MCP server"""
        if self.process:
            try:
                self.process.terminate()
                await self.process.wait()
            except:
                pass



class ClaudeBotWithMCP:
    def __init__(self, mcp_config_file: str = "mcp_config.json"):
        # Discord bot setup
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        
        self.bot = commands.Bot(command_prefix='!', intents=intents)
        
        # Claude API client
        self.claude = anthropic.Anthropic(
            api_key=os.getenv('ANTHROPIC_API_KEY')
        )
        
        # MCP clients
        self.mcp_clients = {}
        self.all_tools = []
        
        # NEW: Conversation memory storage
        # Store conversation history per channel/user
        # Key format: "channel_id" for channels, "user_id" for DMs
        self.conversation_history = defaultdict(lambda: deque(maxlen=10))  # Store 10 messages (5 pairs)
        
        # Load MCP configuration
        self.load_mcp_config(mcp_config_file)
        
        # Setup event handlers
        self.setup_events()
        self.setup_commands()
    
    # NEW: Helper methods for conversation context management
    def get_conversation_key(self, context):
        """Generate a unique key for storing conversation history"""
        if hasattr(context, 'guild') and context.guild:
            # For server channels, use channel_id
            return f"channel_{context.channel.id}"
        else:
            # For DMs, use user_id
            return f"user_{context.author.id}"
    
    def add_to_history(self, conversation_key: str, role: str, content: str):
        """Add a message to conversation history"""
        self.conversation_history[conversation_key].append({
            "role": role,
            "content": content
        })
    
    def get_conversation_messages(self, conversation_key: str, new_message: str):
        """Get conversation history plus the new message"""
        messages = list(self.conversation_history[conversation_key])
        messages.append({"role": "user", "content": new_message})
        return messages
    
    def clear_conversation(self, context):
        """Clear conversation history for a specific channel/user"""
        conversation_key = self.get_conversation_key(context)
        if conversation_key in self.conversation_history:
            del self.conversation_history[conversation_key]
            return True
        return False
    
    def get_conversation_stats(self):
        """Get stats about stored conversations"""
        return {
            "active_conversations": len(self.conversation_history),
            "total_messages": sum(len(history) for history in self.conversation_history.values())
        }
    
    def load_mcp_config(self, config_file: str):
        """Load MCP server configuration"""
        try:
            with open(config_file, 'r') as f:
                self.mcp_config = json.load(f)
            logger.info(f"Loaded MCP config with {len(self.mcp_config.get('servers', []))} servers")
        except FileNotFoundError:
            logger.warning(f"MCP config file {config_file} not found, creating example")
            self.create_example_config(config_file)
            self.mcp_config = {"servers": []}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in MCP config: {e}")
            self.mcp_config = {"servers": []}
    
    def create_example_config(self, config_file: str):
        """Create an example MCP configuration file"""
        example_config = {
            "servers": [
                {
                    "name": "my-custom-server",
                    "docker": {
                        "image": "my-mcp-server:latest",
                        "env": {
                            "API_KEY": "your-api-key-here"
                        },
                        "volumes": [
                            "/host/path:/container/path"
                        ],
                        "args": ["--config", "/container/path/config.json"]
                    }
                },
                {
                    "name": "filesystem",
                    "command": ["python", "-m", "mcp_server_filesystem", "/allowed/path"]
                }
            ]
        }
        
        with open(config_file, 'w') as f:
            json.dump(example_config, f, indent=2)
        
        logger.info(f"Created example MCP config at {config_file}")
    
    async def start_mcp_servers(self):
        """Start all configured MCP servers"""
        for server_config in self.mcp_config.get("servers", []):
            try:
                client = MCPClient(server_config)
                await client.start()
                self.mcp_clients[server_config["name"]] = client
                self.all_tools.extend(client.tools)
            except Exception as e:
                logger.error(f"Failed to start MCP server {server_config['name']}: {e}")
        
        logger.info(f"Started {len(self.mcp_clients)} MCP servers with {len(self.all_tools)} total tools")
    
    def setup_events(self):
        @self.bot.event
        async def on_ready():
            logger.info(f'{self.bot.user} has connected to Discord!')
            logger.info(f'Bot is in {len(self.bot.guilds)} guilds')
            # Start MCP servers after Discord connection
            await self.start_mcp_servers()
        
        @self.bot.event
        async def on_message(message):
            if message.author == self.bot.user:
                return
            
            if self.bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
                await self.handle_claude_message(message)
            
            await self.bot.process_commands(message)
    
    def setup_commands(self):
        @self.bot.command(name='tools')
        async def list_tools(ctx):
            """List available MCP tools"""
            if not self.all_tools:
                await ctx.reply("No MCP tools are currently available.")
                return
            
            tools_info = []
            for tool in self.all_tools:
                tools_info.append(f"**{tool.name}** ({tool.server_name}): {tool.description}")
            
            response = "Available MCP tools:\n" + "\n".join(tools_info)
            if len(response) > 2000:
                response = response[:1900] + "\n... (truncated)"
            
            await ctx.reply(response)
        
        @self.bot.command(name='servers')
        async def list_servers(ctx):
            """List MCP server status"""
            if not self.mcp_clients:
                await ctx.reply("No MCP servers are running.")
                return
            
            status = []
            for name, client in self.mcp_clients.items():
                status.append(f"**{name}**: {len(client.tools)} tools")
            
            await ctx.reply("MCP Servers:\n" + "\n".join(status))
        
        # NEW: Commands for context management
        @self.bot.command(name='clear_context')
        async def clear_context(ctx):
            """Clear conversation context for this channel/DM"""
            if self.clear_conversation(ctx):
                await ctx.reply("✅ Conversation context cleared!")
            else:
                await ctx.reply("No conversation context to clear.")
        
        @self.bot.command(name='context_stats')
        async def context_stats(ctx):
            """Show conversation context statistics"""
            stats = self.get_conversation_stats()
            await ctx.reply(f"📊 **Context Stats:**\n"
                          f"Active conversations: {stats['active_conversations']}\n"
                          f"Total stored messages: {stats['total_messages']}")
    
    async def handle_claude_message(self, message):
        """Handle messages with Claude and MCP integration"""
        try:
            content = message.content
            if self.bot.user.mentioned_in(message):
                content = content.replace(f'<@{self.bot.user.id}>', '').strip()
            
            if not content:
                await message.reply("Hello! How can I help you? I have access to various tools through MCP.")
                return
            
            async with message.channel.typing():
                
                logging.info(content)
                response = await self.get_claude_response_with_tools(content, message)
                logging.info(response)
                # Split long responses
                if len(response) > 2000:
                    chunks = [response[i:i+1900] for i in range(0, len(response), 1900)]
                    for i, chunk in enumerate(chunks):
                        if i == 0:
                            await message.reply(chunk)
                        else:
                            await message.channel.send(chunk)
                else:
                    await message.reply(response)
        
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await message.reply("Sorry, I encountered an error processing your request.")
    
    async def get_claude_response_with_tools(self, user_message: str, context) -> str:
        """Get response from Claude with MCP tool integration"""
        try:
            # NEW: Get conversation key for context management
            conversation_key = self.get_conversation_key(context)
            
            # Convert MCP tools to Claude format
            claude_tools = []
            for tool in self.all_tools:
                claude_tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema
                })
            
            # UPDATED: Enhanced system message with context awareness
            system_message = f"""You are a helpful Discord bot with access to various tools through MCP (Model Context Protocol).

Available tools: {', '.join([tool.name for tool in self.all_tools])}

Context:
- Server: {context.guild.id if hasattr(context, 'guild') and context.guild else 'Direct Message'}
- Channel: {context.channel.id if hasattr(context.channel, 'name') else 'DM'}
- User: {context.author.id if hasattr(context, 'author') else 'Unknown'}

You have access to the recent conversation history. Use this context to provide more relevant and coherent responses.
Use tools when appropriate to help the user. Keep responses conversational and Discord-friendly."""
            
            # UPDATED: Get conversation history with new message instead of single message
            messages = self.get_conversation_messages(conversation_key, user_message)
            
            response = self.claude.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=1000,
                system=system_message,
                messages=messages,
                tools=claude_tools if claude_tools else None
            )
            
            # Process response and handle tool calls
            final_response = ""
            current_messages = messages.copy()  # NEW: Work with a copy for tool calls
            
            for content_block in response.content:
                if content_block.type == "text":
                    final_response += content_block.text
                elif content_block.type == "tool_use":
                    # Execute MCP tool
                    tool_result = await self.execute_mcp_tool(content_block)
                    
                    # UPDATED: Use current_messages instead of messages for tool calls
                    current_messages.append({"role": "assistant", "content": response.content})
                    current_messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": content_block.id,
                            "content": json.dumps(tool_result)
                        }]
                    })
                    
                    # Get follow-up response
                    follow_up = self.claude.messages.create(
                        model="claude-3-haiku-20240307",
                        max_tokens=1000,
                        system=system_message,
                        messages=current_messages,  # UPDATED: Use current_messages
                        tools=claude_tools if claude_tools else None
                    )
                    
                    for block in follow_up.content:
                        if block.type == "text":
                            final_response += block.text
            
            # NEW: Store the conversation in history
            self.add_to_history(conversation_key, "user", user_message)
            self.add_to_history(conversation_key, "assistant", final_response)
            
            return final_response or "I processed your request but don't have a specific response."
        
        except Exception as e:
            logger.error(f"Error getting Claude response: {e}")
            return "Sorry, I encountered an error processing your request with my AI service."
    
    async def execute_mcp_tool(self, tool_use) -> Dict[str, Any]:
        """Execute an MCP tool call"""
        tool_name = tool_use.name
        arguments = tool_use.input
        
        # Find the tool and its server
        tool = None
        for t in self.all_tools:
            if t.name == tool_name:
                tool = t
                break
        
        if not tool:
            return {"error": f"Tool {tool_name} not found"}
        
        client = self.mcp_clients.get(tool.server_name)
        if not client:
            return {"error": f"Server {tool.server_name} not available"}
        
        try:
            result = await client.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}")
            return {"error": str(e)}
    
    async def cleanup(self):
        """Clean up MCP servers"""
        for client in self.mcp_clients.values():
            await client.stop()
        logger.info("Stopped all MCP servers")
    
    def run(self):
        """Start the bot"""
        try:
            self.bot.run(discord_bot_token)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Error running bot: {e}")
        finally:
            # Cleanup
            asyncio.run(self.cleanup())

if __name__ == "__main__":
    bot = ClaudeBotWithMCP()
    bot.run()
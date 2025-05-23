import json
import datetime
from llm import llm
from flight_search import SkyscannerFlightSearchTool
from nlp import extract_flight_parameters

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field



# Create flight search tool
flight_search = SkyscannerFlightSearchTool()
tools = [flight_search]
tool_names = flight_search.name

# ---- 1.1  定义输入 schema（字段直接映射 invoke 里的参数） ----
class FlightSearchInput(BaseModel):
    origin: str         = Field(..., description="IATA code or city name of departure")
    destination: str    = Field(..., description="IATA code or city name of arrival")
    departure_date: str = Field(..., description="YYYY-MM-DD")
    return_date: str | None = Field(None, description="YYYY-MM-DD if round‑trip")
    adults: int = Field(1, ge=1, description="Number of adult passengers")
    children: int = 0
    infants: int = 0
    cabin_class: str = Field("Economy", description="Economy | Premium Economy | Business | First")

# ---- 1.2  包装现有 invoke ----
flight_tool = StructuredTool.from_function(
    name        = "flight_search",
    description = "Search for flights via Skyscanner and return formatted text.",
    func        = flight_search.invoke,   # 你的旧函数，保持不变
    args_schema = FlightSearchInput,
    return_direct = True                  # 让结果直接写回对话
)

# Prepare tool descriptions
tool_descs = []
args_desc = []
for name, info in flight_search.args.items():
    args_desc.append({
        'name': name,
        'description': info.get('description', ''),
        'type': info['type']
    })
args_desc = json.dumps(args_desc, ensure_ascii=False)
tool_descs.append(f"{flight_search.name}: {flight_search.description}, args: {args_desc}")
tool_descs = '\n'.join(tool_descs)

# Updated prompt template to support both one-way and round-trip
prompt_tpl = '''Today is {today}. Please help the user with their flight search request. You have access to the following tool:

{tool_descs}

These are chat history before:
{chat_history}

IMPORTANT: You MUST use the exact format including "Thought: I now know the final answer" followed by "Final Answer: ..."

Use the following format:

Question: the input question you must answer
Thought: Think step by step to understand what information the user is looking for. For flight searches, carefully identify:
  1. Origin and destination airports/cities 
  2. Travel dates (departure and return if applicable)
  3. Trip type (one-way or round-trip)
  4. Number of passengers (adults, children, infants)
  5. Cabin class preferences (Economy, Premium Economy, Business)
  6. Any other specific requirements (non-stop flights, price range, etc.)
Action: flight_search
Action Input: the input to the action - use valid JSON format with quotes around string values, e.g., {{"key": "value", "number": 42}}. For round-trip flights, include both departure_date and return_date. Do NOT use markdown code blocks.
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can be repeated if needed)
Thought: I now know the final answer
Final Answer: Present the flight information in a clear, organized manner. For each flight option:

For one-way flights:
- Airline and flight number
- Departure and arrival times
- Duration
- Cabin class
- Price
- Number of stops
- Booking link (click to book this flight directly)

For round-trip flights:
- Outbound: Airline, flight number, departure/arrival times, duration, stops
- Return: Airline, flight number, departure/arrival times, duration, stops
- Total price
- Cabin class
- Booking link (click to book this itinerary directly)

For the user, explain if any information was missing from their query and what assumptions you made.

Begin!

Question: {query}
{agent_scratchpad}
'''

# Simplified agent execution logic (single tool)
def agent_execute(query, chat_history=[]):
    global flight_search, tool_names, tool_descs, prompt_tpl

    # Preprocess flight queries
    # Extract flight parameters to streamline processing
    flight_params = extract_flight_parameters(query)
    if flight_params:
        # Enhance query with structured parameters
        query_addition = f"\n\nExtracted parameters: {json.dumps(flight_params, indent=2)}"
        query = f"{query}{query_addition}"
    
    agent_scratchpad = ''
    while True:
        history = '\n'.join([f"Question:{h[0]}\nAnswer:{h[1]}" for h in chat_history])
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        prompt = prompt_tpl.format(
            today=today,
            chat_history=history,
            tool_descs=tool_descs,
            tool_names=tool_names,
            query=query,
            agent_scratchpad=agent_scratchpad
        )

        print('\033[32m---Waiting for LLM response...\033[0m', flush=True)
        response = llm(prompt, user_stop_words=['Observation:'])
        print('\033[34m---LLM Response---\n%s\n---\033[34m' % response, flush=True)

        # Parse response content
        thought_i = response.rfind('Thought:')
        final_answer_i = response.rfind('\nFinal Answer:')
        action_i = response.rfind('\nAction:')
        action_input_i = response.rfind('\nAction Input:')
        observation_i = response.rfind('\nObservation:')

        if final_answer_i != -1 and thought_i < final_answer_i:
            final_answer = response[final_answer_i + len('\nFinal Answer:'):].strip()
            chat_history.append((query, final_answer))
            return True, final_answer, chat_history

        if not (thought_i < action_i < action_input_i):
            # If the format is incorrect, generate a fallback response
            fallback_response = "I'm having trouble processing your flight search request. Could you please provide more specific details like origin, destination, travel dates, and the number of passengers?"
            chat_history.append((query, fallback_response))
            return True, fallback_response, chat_history
            
        if observation_i == -1:
            observation_i = len(response)
            response = response + '\nObservation: '

        thought = response[thought_i + len('Thought:'):action_i].strip()
        action = response[action_i + len('\nAction:'):action_input_i].strip()
        action_input = response[action_input_i + len('\nAction Input:'):observation_i].strip()

        # Check if action is flight_search
        if action != "flight_search":
            observation = 'Invalid tool specified. Please use "flight_search".'
            agent_scratchpad += response + observation + '\n'
            continue

        try:
            # Use flight_search tool with provided input
            tool_ret = flight_search.invoke(input=action_input)
        except Exception as e:
            observation = f'Tool execution error: {e}'
        else:
            observation = str(tool_ret)

        agent_scratchpad += response + observation + '\n'

# Agent retry with improved error handling
def agent_execute_with_retry(query, chat_history=[], retry_times=2):
    for i in range(retry_times):
        try:
            success, result, chat_history = agent_execute(query, chat_history=chat_history)
            if success:
                return success, result, chat_history
        except Exception as e:
            if i == retry_times - 1:
                error_msg = f"I apologize, but I'm having trouble processing your request due to a technical issue. Could you please try rephrasing your question or providing more specific flight details?"
                chat_history.append((query, error_msg))
                return False, error_msg, chat_history
    
    # If all retries fail but no exception
    fallback_msg = "I couldn't find the flight information you requested. Could you please provide more details about your trip, including specific airports or cities, travel dates, and passenger information?"
    chat_history.append((query, fallback_msg))
    return False, fallback_msg, chat_history
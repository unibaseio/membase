# Membase: Decentralized Memory Layer for AI Agents

**Membase** is a high-performance decentralized AI memory layer designed for persistent conversation storage, scalable knowledge bases, and secure on-chain collaboration tasks — built for the next generation of intelligent agents.

---

## ✨ Features

- **On-Chain Identity Management**  
  Secure cryptographic identity verification and agent registration on blockchain, enabling trustless collaboration, verifiable interactions, and autonomous task coordination in decentralized multi-agent ecosystems.

- **Multi-Memory Management**  
  Manage multiple conversation threads with preload and auto-upload support to Membase Hub.

- **Buffered Single Memory**  
  Store and sync a conversation history with decentralized storage hubs.

- **Knowledge Base Integration**  
  Build, expand, and synchronize agent knowledge using Chroma-based vector storage.

- **Chain Task Coordination**  
  Create, join, and settle on-chain collaborative tasks with decentralized rewards.

- **Secure and Scalable**  
  Designed for millions of conversations and knowledge objects, with blockchain-based verification.

- **Long-Term Memory (LTM)**  
  Automatically summarizes and condenses short-term conversation history into structured long-term memory for efficient retrieval, reasoning, and user modeling.

---

# 🚀 Quick Start

## Installation

```bash
pip install git+https://github.com/unibaseio/membase.git
# or clone locally
git clone https://github.com/unibaseio/membase.git
cd membase
pip install -e .
```

---

# ⛓️ Identity Register

- Environment Variables

```bash
export MEMBASE_ID="<any unique string>"
export MEMBASE_ACCOUNT="<account address>"
export MEMBASE_SECRET_KEY="<account secret>"
```

- Registeration and Verification

```python
from membase.chain.chain import membase_chain

# register onchain
agent_name = "your_agent_name"
membase_chain.register(agent_name)

# buy auth, then new agent can visit agent_name's resource
new_agent = "another_agent_name"
membase_chain.buy(agent_name, new_agent)

# check persmission
# get address and valid sign with new_agent_address
new_agent_address = membase_chain.get_agent(new_agent)
valid_sign(sign, new_agent_address)
# check onchain persmission
if membase_chain.has_auth(agent_name, new_agent):
  print("has permission")
```

# 🧠 Multi-Memory Example

Manage multiple conversation threads simultaneously.

```python
from membase.memory.multi_memory import MultiMemory
from membase.memory.message import Message

mm = MultiMemory(
    membase_account="default",
    auto_upload_to_hub=True,
    preload_from_hub=True
)

msg = Message(
    name="agent9527",
    content="Hello! How can I help you?",
    role="assistant",
    metadata="help info"
)

conversation_id = 'your_conversation'
mm.add(msg, conversation_id)
```

---

# 🧠 Long-Term Memory (LTM) Example

LTM (Long-Term Memory) automatically summarizes and condenses short-term memory (STM) into structured, high-value long-term memory for each conversation. This enables efficient retrieval, knowledge accumulation, and user profile modeling. LTM is generated in the background when enough new messages are added.

```python
import time
import uuid
from membase.memory.lt_memory import LTMemory
from membase.memory.message import Message

# 1. Create LTMemory instance
test_account = "test_account_" + str(uuid.uuid4())
ltm = LTMemory(membase_account=test_account, auto_upload_to_hub=True)
conv_id = ltm.default_conversation_id
name = str(uuid.uuid4())

# 2. Synthesize 20 rounds of Q&A
qa_pairs = [
    ("What does it mean to learn efficiently?", "Learning efficiently means acquiring knowledge or skills in a way that maximizes results while minimizing wasted time and effort."),
    ("Why is efficient learning important?", "Efficient learning helps you achieve your goals faster, retain information better, and reduces frustration and burnout."),
    # ... (add more Q&A pairs as needed)
]
messages = []
for i, (q, a) in enumerate(qa_pairs):
    user_msg = Message(name=name, content=f"Question {i+1}: {q}", role="user")
    assistant_msg = Message(name=name, content=f"Answer {i+1}: {a}", role="assistant")
    messages.extend([user_msg, assistant_msg])

# 3. Insert messages into memory
for msg in messages:
    ltm.add(msg, conversation_id=conv_id)
    time.sleep(1)  # Ensure timestamps are unique

# 4. Wait for background summarization (ltm/profile)
print("Waiting for ltm/profile generation...")
time.sleep(120)  # LTM summarization runs every 60 seconds

# 5. Fetch LTM
ltm_list = ltm.get_ltm(conversation_id=conv_id, recent_n=1)
print("\nLTM:")
for msg in ltm_list:
    print(msg.content)

# 6. Fetch profile
profile_list = ltm.get_profile(recent_n=1)
print("\nProfile:")
for msg in profile_list:
    print(msg.content)

# 7. Stop background thread
ltm.stop()
```

---

# 🔍 LTM Knowledge Retrieval Example

You can retrieve relevant knowledge from all stored memories (STM, LTM, profile, etc.) using the LTM's `retrieve` interface, which leverages the built-in knowledge base (Chroma).

```python
# Retrieve top-3 relevant documents for a query
results = ltm.retrieve(
    query="efficient learning strategies",
    top_k=3
)
for doc in results:
    print(doc.content)
    print(doc.metadata)
```

You can also use advanced options such as `similarity_threshold`, `metadata_filter`, or `content_filter` for more precise retrieval.

---

🌐 Hub Access: Visit your conversations at [https://testnet.explorer.unibase.com/](https://testnet.explorer.unibase.com/)

---

# 🗂️ Single Memory Example

Manage a single conversation buffer.

```python
from membase.memory.message import Message
from membase.memory.buffered_memory import BufferedMemory

memory = BufferedMemory(membase_account="default", auto_upload_to_hub=True)
msg = Message(
    name="agent9527",
    content="Hello! How can I help you?",
    role="assistant",
    metadata="help info"
)
memory.add(msg)
```

---

# 📚 Knowledge Base Example

Store and manage knowledge documents using Chroma integration.

```python
from membase.knowledge.chroma import ChromaKnowledgeBase
from membase.knowledge.document import Document

kb = ChromaKnowledgeBase(
    persist_directory="/tmp/test",
    membase_account="default",
    auto_upload_to_hub=True
)

doc = Document(
    content="The quick brown fox jumps over the lazy dog.",
    metadata={"source": "test", "date": "2025-03-05"}
)

kb.add_documents(doc)
```

---

# 🔗 Chain Task Example

Coordinate collaborative tasks with staking and settlement on-chain.

## Environment Variables

```bash
export MEMBASE_ID="<any unique string>"
export MEMBASE_ACCOUNT="<account address>"
export MEMBASE_SECRET_KEY="<account secret>"
```

## Code Example

```python
from membase.chain.chain import membase_chain

task_id = "task0227"
price = 100000

# Create a new collaborative task
membase_chain.createTask(task_id, price)

# Agent "alice" joins and stakes
agent_id = "alice"
membase_chain.register(agent_id)
membase_chain.joinTask(task_id, agent_id)

# Agent "bob" joins and stakes
agent_id = "bob"
membase_chain.register(agent_id)
membase_chain.joinTask(task_id, agent_id)

# Task owner finishes and distributes rewards
membase_chain.finishTask(task_id, agent_id="alice")

# Query task info
membase_chain.getTask(task_id)
```

---

# 📜 License

MIT License. See [LICENSE](./LICENSE) for details.

---

# 📞 Contact

- Website: [https://www.unibase.com](https://www.unibase.com)
- GitHub Issues: [Membase Issues](https://github.com/unibaseio/membase/issues)
- Email: <support@unibase.com>

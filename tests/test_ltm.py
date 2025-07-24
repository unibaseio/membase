import time
import os
import uuid
from membase.memory.lt_memory import LTMemory
from membase.memory.message import Message


def test_ltm_and_profile_generation():
    # 1. Create LTMemory instance
    ltm = LTMemory(membase_account="test_account_" + str(uuid.uuid4()) , auto_upload_to_hub=True)
    conv_id = ltm.default_conversation_id
    name = str(uuid.uuid4())

    # 2. Synthesize 20 rounds of Q&A
    messages = []
    topic = "How to learn efficiently"
    qa_pairs = [
        ("What does it mean to learn efficiently?", "Learning efficiently means acquiring knowledge or skills in a way that maximizes results while minimizing wasted time and effort."),
        ("Why is efficient learning important?", "Efficient learning helps you achieve your goals faster, retain information better, and reduces frustration and burnout."),
        ("What are the first steps to becoming an efficient learner?", "Start by setting clear, specific goals and understanding your preferred learning style."),
        ("How do I identify my learning style?", "Reflect on past experiences: do you learn best by reading, listening, doing, or visualizing? Try different methods and observe which helps you retain information most."),
        ("Are there universal strategies that help everyone learn better?", "Some strategies, like active recall, spaced repetition, and summarizing in your own words, benefit most learners regardless of style."),
        ("Can you explain active recall and how to use it?", "Active recall involves testing yourself on the material, forcing your brain to retrieve information, which strengthens memory and understanding."),
        ("What is spaced repetition and why is it effective?", "Spaced repetition means reviewing material at increasing intervals over time, which combats forgetting and improves long-term retention."),
        ("How can I apply spaced repetition in my daily study routine?", "Use tools like flashcards or spaced repetition apps, and schedule reviews of material at 1 day, 3 days, 1 week, and so on after first learning it."),
        ("How important is taking breaks during study sessions?", "Regular breaks prevent mental fatigue and help consolidate memories. The Pomodoro Technique (25 min study, 5 min break) is a popular method."),
        ("What role does sleep play in efficient learning?", "Sleep is crucial for memory consolidation. Getting enough quality sleep after learning helps your brain store new information."),
        ("How can I stay motivated to keep learning efficiently?", "Set achievable milestones, track your progress, reward yourself, and remind yourself of your long-term goals."),
        ("What should I do if I feel stuck or not making progress?", "Change your approach: try different resources, ask for help, or teach the material to someone else to deepen your understanding."),
        ("Is it better to study alone or with others?", "Both have benefits. Studying alone allows focus, while group study can provide new perspectives and clarify doubts."),
        ("How can technology help with efficient learning?", "Use educational apps, online courses, and digital note-taking tools to organize information and access diverse resources."),
        ("How do I avoid distractions while studying?", "Create a dedicated study space, turn off notifications, and set specific times for focused work."),
        ("What is the value of reviewing and reflecting on what I’ve learned?", "Reviewing and reflecting helps reinforce knowledge, identify gaps, and connect new information to what you already know."),
        ("How can I measure my learning progress?", "Regular self-testing, tracking completed topics, and applying knowledge in real situations are good ways to measure progress."),
        ("What should I do after reaching a learning goal?", "Celebrate your achievement, then set new goals to continue growing and applying what you’ve learned."),
        ("How can I maintain efficient learning habits long-term?", "Make learning a routine, stay curious, and periodically update your strategies to keep them effective."),
        ("Any final tips for someone who wants to master efficient learning?", "Stay consistent, be patient with yourself, and remember that learning is a lifelong journey. Adapt and enjoy the process!")
    ]
    for i, (q, a) in enumerate(qa_pairs):
        user_msg = Message(name=name, content=f"Question {i+1}: {q}", role="user")
        assistant_msg = Message(name=name, content=f"Answer {i+1}: {a}", role="assistant")
        messages.extend([user_msg, assistant_msg])

    # 3. Insert messages into memory
    for msg in messages:
        ltm.add(msg, conversation_id=conv_id)
        time.sleep(1)

    # 4. Wait for background summarization (ltm/profile)
    print("Waiting for ltm/profile generation...")
    time.sleep(120)  # 归纳线程每60秒检查一次，保险起见多等10秒

    # 5. Fetch ltm
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

if __name__ == "__main__":
    test_ltm_and_profile_generation()

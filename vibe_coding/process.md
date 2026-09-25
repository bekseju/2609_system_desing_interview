# 단계별 Process

## 1단계: Specification 작성

- 아래 예시와 같은 prompt를 활용하여 specification.md를 생성한다.
- 생성된 specification이 내가 원하는 내용에 부합하도록 수정한다.

```
I need to write a software specification, and I need your help with writing it.

I want to build a flashcards app to help learn Spanish in the form of a web application using TypeScript and Vite/React.

Here are some features I think I will need:

Each card should be in Spanish, and I need to know the word in English
It should have the ability to flip cards revealing the word in English
After flipping, below each card I should have two buttons, ask if I got it wrong or right
Remember which cards I got wrong
Feature: to redo only cards I don’t know.
Quiz/Test Mode: Multiple choice or fill-in-the-blank options.
Statistics page: Track how many cards you've studied, correct vs. incorrect answers

The output should be formatted as Markdown.

Ask follow-up questions if something is unclear BEFORE you start generating.
```

## 2단계: TODO 작성

- 위 생성된 specification을 사용해서 한번에 application을 만들 수도 있지만 vibe coding은 단계별로 잘게 쪼개어 만들어보면서 확인하는 방식으로 생성해야 한다.
- 따라서 위 specification을 기반으로 단계별로 app을 만들어 나갈 수 있도록 tood.md 파일을 생성하도록 한다.

```
Take this specification and create a TODO list with checkboxes to help implement one feature at a time. Order the features from easy to hard in phases. For each item in the list, also define acceptance criteria.  The output should be in Markdown format.
```

## 3단계: Vibe Coding 시작

- 절대 한번에 모든걸 다 만드려 하지 말고, 단게별로 진행하고 계속해서 검증하자.
- 위의 1, 2단계는 굳이 VSCode 안의 AI Assistant에서 진행할 필요는 없지만, 이 단계부터는 VSCode 안에서 진행하자.
- 만약 Claude라면 Context로 specification.md와 TODO.md를 지정해주자.

```
Use the software requirements (specification.md) and the TODO list (TODO.md) to complete Phase 1 of the project in the current directory. Mark items as done in the TODO list only after verifying that the acceptance criteria have been met.
```

- Agent가 실행되는 과정에 필요한 승인을 해주고, 생성된 결과를 확인한다.
- 생성된 결과에 수정이 필요하다면 Agent에 수정 명령을 줘서 수정한다.
